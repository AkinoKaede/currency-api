"""Explicit, configurable USD-parity assets; never inferred from ticker text."""

from collections.abc import Mapping
import json
from pathlib import Path
import re
import sysconfig


def validate_stablecoins(coins: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(coins, Mapping):
        raise ValueError("Stablecoins must be a mapping of currency keys to names")
    normalized = {}
    for code, name in coins.items():
        if not isinstance(code, str):
            raise ValueError("Stablecoin currency key must be a string")
        key = code.lower()
        if (len(key) > 64 or not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", key)
                or key.endswith(".min") or key in {"usd", "date", "currencies", "metadata"}):
            raise ValueError(f"Invalid or reserved stablecoin currency key: {code}")
        if key in normalized:
            raise ValueError(f"Duplicate stablecoin currency key: {key}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Stablecoin {code} requires a display name")
        normalized[key] = name.strip()
    return dict(sorted(normalized.items()))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate stablecoin configuration key: {key}")
        result[key] = value
    return result


def load_stablecoins(path: Path | None = None) -> dict[str, str]:
    if path is None:
        source = Path(__file__).resolve().parents[2] / "config/stablecoins.json"
        installed = Path(sysconfig.get_path("data")) / "share/currency-api/stablecoins.json"
        path = source if source.is_file() else installed
    config = json.loads(Path(path).read_bytes(), object_pairs_hook=_unique_object)
    if (not isinstance(config, dict) or type(config.get("schema_version")) is not int
            or config["schema_version"] != 1 or config.get("policy") != "fixed_usd_parity"):
        raise ValueError("Stablecoin configuration requires schema_version=1 and fixed_usd_parity policy")
    entries = config.get("coins")
    if not isinstance(entries, dict):
        raise ValueError("Stablecoin configuration requires a coins object")
    names, enabled = {}, set()
    for code, entry in entries.items():
        if not isinstance(entry, dict) or type(entry.get("enabled", True)) is not bool:
            raise ValueError(f"Invalid stablecoin configuration entry: {code}")
        names[code] = entry.get("name")
        if entry.get("enabled", True):
            enabled.add(code.lower())
    # Validate disabled entries too so enabling one cannot expose malformed paths.
    names = validate_stablecoins(names)
    return {code: name for code, name in names.items() if code in enabled}
