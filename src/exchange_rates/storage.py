"""Validated daily files and independent latest currency-api views."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from .currency_api import DIRECTIONS, PROVIDERS, PROVIDER_PATHS, build_currency_api
from .models import DayResult, json_bytes
from .stablecoins import load_stablecoins, validate_stablecoins


_MEMBER = re.compile(
    r"(?P<role>history|raw/history|metadata/history)/(?P<year>\d{4})/"
    r"(?P<month>\d{2})/(?P<provider>[a-z][a-z0-9_-]*)/"
    r"(?P<day>\d{4}-\d{2}-\d{2})(?:\.json|\.parts/"
    r"(?P<part>shard-(?:0[1-9]|[1-9][0-9])\.json))\Z"
)


_ROLES = {"history", "raw/history", "metadata/history"}


class StorageError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise StorageError(f"Symlink output path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    pending = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def member_info(name: str, period: str | None = None) -> tuple[str, str, str]:
    match = _MEMBER.fullmatch(name)
    if not match or (match["part"] and match["role"] != "raw/history"):
        raise StorageError(f"Unexpected history path: {name}")
    try:
        day = date.fromisoformat(match["day"])
    except ValueError as exc:
        raise StorageError(f"Invalid history date: {name}") from exc
    if f"{day.year:04d}" != match["year"] or f"{day.month:02d}" != match["month"]:
        raise StorageError(f"History date/path mismatch: {name}")
    if period is not None and not day.isoformat().startswith(period + "-"):
        raise StorageError(f"History entry outside period {period}: {name}")
    return match["role"], match["provider"], day.isoformat()


def _raw_index(body: bytes | bytearray, overflow: bool = False) -> dict | None:
    if overflow:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(value, dict) and value.get("format") == "exchange-rates-raw-shards-v1":
        return value
    return None


def _validate_groups(entries: dict[str, dict], indices: dict[str, dict], period: str) -> None:
    groups: dict[tuple[str, str], set[str]] = {}
    shards: dict[tuple[str, str], set[str]] = {}
    for name in entries:
        role, provider, day = member_info(name, period)
        pair = provider, day
        if ".parts/" in name:
            shards.setdefault(pair, set()).add(name.rsplit("/", 1)[1])
        else:
            groups.setdefault(pair, set()).add(role)
    if not entries:
        raise StorageError(f"Empty daily file group: {period}")
    if set(shards) - set(groups):
        raise StorageError("Orphan raw parts")
    for pair, roles in groups.items():
        if roles != _ROLES:
            raise StorageError(f"Incomplete normalized/raw/metadata group: {pair}")
        provider, day = pair
        index_name = f"raw/history/{day[:4]}/{day[5:7]}/{provider}/{day}.json"
        index = indices.get(index_name)
        if index is None:
            if shards.get(pair):
                raise StorageError(f"Raw parts without shard index: {pair}")
            continue
        if (index.get("provider") != provider or index.get("requested_date") != day
                or index.get("parts_directory") != f"{day}.parts"):
            raise StorageError(f"Raw index identity/path mismatch: {pair}")
        parts = index.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= 99:
            raise StorageError(f"Raw index must reference 1 to 99 parts: {pair}")
        expected = {f"shard-{index:02d}.json" for index in range(1, len(parts) + 1)}
        names = set()
        for part in parts:
            if (not isinstance(part, dict) or not isinstance(part.get("name"), str)
                    or part["name"] not in expected or part["name"] in names):
                raise StorageError(f"Invalid/duplicate raw part name: {pair}")
            names.add(part["name"])
            path = index_name[:-5] + ".parts/" + part["name"]
            entry = entries.get(path)
            if (entry is None or type(part.get("size")) is not int or part["size"] != entry["size"]
                    or part.get("sha256") != entry["sha256"]):
                raise StorageError(f"Raw part size/hash mismatch: {path}")
        if names != expected or shards.get(pair, set()) != names:
            raise StorageError(f"Missing/orphan raw part: {pair}")


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents):
        raise StorageError(f"Data input must be a regular file: {path}")


class DataStore:
    def __init__(self, root: Path, *, stablecoins: Mapping[str, str] | None = None):
        self.stablecoins = load_stablecoins() if stablecoins is None else validate_stablecoins(stablecoins)
        if root.is_symlink():
            raise StorageError("Data root must not be a symlink")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in self.root.rglob("*")):
            raise StorageError("Data tree must not contain symlinks")


    def _paths(self, result: DayResult) -> dict[str, bytes]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", result.provider) or type(result.requested_date) is not date:
            raise StorageError("Invalid provider/date")
        day = result.requested_date
        suffix = f"{day.year:04d}/{day.month:02d}/{result.provider}/{day.isoformat()}"
        metadata = dict(result.metadata)
        metadata.update({"provider": result.provider, "requested_date": day.isoformat()})
        raw = result.raw
        parts = getattr(result, "raw_parts", {})
        content = {f"history/{suffix}.json": result.normalized,
                   f"metadata/history/{suffix}.json": json_bytes(metadata)}
        if parts:
            index = _raw_index(raw)
            if index is None:
                raise StorageError("Raw parts require shard index")
            index["parts_directory"] = f"{day.isoformat()}.parts"
            raw = json_bytes(index)
            for name, body in parts.items():
                content[f"raw/history/{suffix}.parts/{name}"] = body
        content[f"raw/history/{suffix}.json"] = raw
        entries = {name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                   for name, body in content.items()}
        index = _raw_index(raw)
        _validate_groups(entries, {f"raw/history/{suffix}.json": index} if index is not None else {}, f"{day.year:04d}-{day.month:02d}")
        return content


    def _remove(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved == self.root or self.root not in resolved.parents or path.is_symlink():
            raise StorageError(f"Refusing removal outside data root: {path}")
        if path.exists():
            path.unlink()
        parent = path.parent
        while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


    def _write_currency_api(self, provider: str, files: dict[str, bytes]) -> None:
        for name, body in files.items():
            atomic_write(self.root / name, body)
        # Retire currencies that are no longer present instead of serving old rates.
        for direction in DIRECTIONS:
            for path in (self.root / PROVIDER_PATHS[provider] / direction / "v1/currencies").glob("*.json"):
                if path.relative_to(self.root).as_posix() not in files:
                    self._remove(path)


    def export_currency_api(self, provider: str) -> None:
        """Rebuild the compatibility view from an already collected latest day."""
        if provider not in PROVIDERS:
            raise ValueError(f"Unsupported currency-api provider: {provider}")
        metadata = json.loads((self.root / "metadata/latest" / f"{provider}.json").read_bytes())
        if metadata.get("provider", provider) != provider:
            raise ValueError("Source metadata provider does not match the requested provider")
        result = DayResult(
            provider, date.fromisoformat(metadata["requested_date"]),
            (self.root / "latest" / f"{provider}.json").read_bytes(), b"", metadata,
        )
        self._write_currency_api(provider, build_currency_api(result, self.stablecoins))


    def _latest(self, result: DayResult, content: dict[str, bytes],
                compatibility: dict[str, bytes]) -> None:
        metadata = self.root / "metadata/latest" / f"{result.provider}.json"
        if metadata.exists():
            previous = date.fromisoformat(json.loads(metadata.read_bytes())["requested_date"])
            if result.requested_date < previous:
                return
        for name, body in content.items():
            role, _, _ = member_info(name)
            prefix = role.split("history", 1)[0]
            if ".parts/" in name:
                path = self.root / "raw/latest" / f"{result.provider}.parts" / name.rsplit("/", 1)[1]
            else:
                path = self.root / prefix / "latest" / f"{result.provider}.json"
                if role == "raw/history" and getattr(result, "raw_parts", {}):
                    index = json.loads(body)
                    index["parts_directory"] = f"{result.provider}.parts"
                    body = json_bytes(index)
            atomic_write(path, body)
        expected = set(getattr(result, "raw_parts", {}))
        for path in (self.root / "raw/latest" / f"{result.provider}.parts").glob("*"):
            if path.name not in expected:
                self._remove(path)
        if compatibility:
            self._write_currency_api(result.provider, compatibility)


    def save_day(self, result: DayResult) -> None:
        content = self._paths(result)
        compatibility = build_currency_api(result, self.stablecoins) if result.provider in PROVIDERS else {}
        for name, body in content.items():
            atomic_write(self.root / name, body)
        day = result.requested_date
        directory = self.root / f"raw/history/{day.year:04d}/{day.month:02d}/{result.provider}/{day.isoformat()}.parts"
        for path in directory.glob("*"):
            if path.relative_to(self.root).as_posix() not in content:
                self._remove(path)
        self._latest(result, content, compatibility)
