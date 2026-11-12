from __future__ import annotations

import argparse
import json
import re
import sys
import sysconfig
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .concurrency import SHARD_COUNT
from .currency_api import PROVIDERS as CURRENCY_API_PROVIDERS, PROVIDER_PATHS
from .diagnostics import Diagnostics
from .http import HttpClient
from .models import json_bytes
from .providers import (
    JcbUnavailableError, fetch_day, fetch_shard, merge_shards, preflight_mastercard,
    prepare_catalog, provider_ids,
)
from .publishing import BranchPublisher, write_summary
from .rate_control import finite_seconds
from .storage import DataStore, atomic_write
from .stablecoins import load_stablecoins

SHARDABLE = ("visa", "mastercard")


def beijing_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def parse_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return parsed


def dates(start: str, end: str) -> tuple[date, date]:
    first = parse_date(start) if start else beijing_today()
    last = parse_date(end) if end else first
    if last < first:
        raise ValueError("End date must not precede start date")
    return first, last


def load_config(path: Path | None) -> dict:
    if path is None:
        source = Path(__file__).resolve().parents[2] / "config" / "providers.json"
        installed = Path(sysconfig.get_path("data")) / "share" / "currency-api" / "providers.json"
        path = source if source.is_file() else installed
    config = json.loads(path.read_bytes())
    if not isinstance(config, dict):
        raise ValueError("Project configuration must be a JSON object")
    for provider in provider_ids():
        if not isinstance(config.get("providers", {}).get(provider), dict):
            raise ValueError(f"Missing provider configuration: {provider}")
        rounds = config["providers"][provider].get("recovery_rounds", 2)
        if type(rounds) is not int or rounds < 0:
            raise ValueError(f"{provider}.recovery_rounds must be a non-negative integer")
    return config


def non_negative_seconds(value: str) -> float:
    try:
        return finite_seconds("request interval", float(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def shard_bundle_path(root: Path, provider: str, target: date, index: int) -> Path:
    return Path(root) / provider / target.isoformat() / f"shard-{index + 1:02d}.json"


def load_shard_bundles(root: Path, provider: str, target: date) -> list[dict]:
    """Read a contiguous shard set produced by parallel shard jobs."""
    directory = Path(root) / provider / target.isoformat()
    if not directory.is_dir():
        raise ValueError(f"No shard directory for {provider} {target.isoformat()}")
    indexed = []
    for path in sorted(directory.glob("shard-*.json")):
        match = re.fullmatch(r"shard-(\d{2})\.json", path.name)
        if match is None:
            raise ValueError(f"Unexpected shard file: {path.name}")
        indexed.append((int(match.group(1)), path))
    numbers = [number for number, _ in indexed]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"Incomplete shard set for {target.isoformat()}: {numbers}")
    bundles = []
    for number, path in indexed:
        try:
            bundle = json.loads(path.read_bytes())
        except (ValueError, UnicodeError, OSError) as exc:
            raise ValueError(f"Unreadable shard {path.name}: {exc}") from exc
        if not isinstance(bundle, dict):
            raise ValueError(f"Shard {path.name} is not a JSON object")
        bundles.append(bundle)
    return bundles


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Manual official exchange-rate collection")
    commands = command.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser(
        "preflight", help="Check Mastercard rate dates before full collection")
    preflight.add_argument("--provider", choices=("mastercard",), required=True)
    preflight.add_argument("--start-date", default="")
    preflight.add_argument("--end-date", default="")
    preflight.add_argument("--config", type=Path)
    collect = commands.add_parser("fetch", help="Collect an inclusive date range in one process")
    collect.add_argument("--provider", choices=provider_ids(), required=True)
    collect.add_argument("--start-date", default="")
    collect.add_argument("--end-date", default="")
    collect.add_argument("--config", type=Path)
    collect.add_argument("--interval", type=non_negative_seconds, default=None,
                         help="Per-session request interval in seconds; overrides configured interval")
    shard = commands.add_parser(
        "fetch-shard", help="Collect one currency shard of a date range")
    shard.add_argument("--provider", choices=SHARDABLE, required=True)
    shard.add_argument("--start-date", default="")
    shard.add_argument("--end-date", default="")
    shard.add_argument("--config", type=Path)
    shard.add_argument("--interval", type=non_negative_seconds, default=None,
                       help="Per-session request interval in seconds; overrides configured interval")
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--shard-count", type=int, default=SHARD_COUNT,
                       help=f"Total disjoint shards (default {SHARD_COUNT})")
    shard.add_argument("--shard-dir", type=Path, required=True,
                       help="Directory shared by shard jobs and the merge job")
    merge = commands.add_parser(
        "merge-shards", help="Verify shard bundles and publish complete days")
    merge.add_argument("--provider", choices=SHARDABLE, required=True)
    merge.add_argument("--start-date", default="")
    merge.add_argument("--end-date", default="")
    merge.add_argument("--config", type=Path)
    merge.add_argument("--shard-dir", type=Path, required=True)
    export = commands.add_parser(
        "export-currency-api", help="Export a card network's latest rates for GMPay / currency-api")
    export.add_argument("--provider", choices=CURRENCY_API_PROVIDERS, required=True)
    for subcommand in (collect, merge, export):
        subcommand.add_argument("--data-dir", type=Path, default=Path("rates-data"))
        subcommand.add_argument("--stablecoins-config", type=Path,
                                help="USD-parity registry; defaults to bundled config/stablecoins.json")
    for subcommand in (collect, shard, merge, preflight):
        subcommand.add_argument("--diagnostics-dir", type=Path, default=Path("diagnostics"))
    for subcommand in (collect, merge):
        subcommand.add_argument("--publish", action="store_true")
        subcommand.add_argument("--remote", default="origin")
        subcommand.add_argument("--branch", default="data")
    return command


def _emit_summary(text: str) -> None:
    try:
        write_summary(text)
    except OSError:
        print("Step summary unavailable; consult diagnostic report files", file=sys.stderr)


def http_settings(config: dict, args: argparse.Namespace) -> dict:
    settings = {**config.get("http", {}),
                **config["providers"][args.provider].get("http", {})}
    if getattr(args, "interval", None) is not None:
        settings["interval"] = args.interval
    return settings


def _run_shard(config: dict, args: argparse.Namespace, diagnostics: Diagnostics,
               first: date, last: date, settings: dict) -> int:
    provider, index, count = args.provider, args.shard_index, args.shard_count
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("shard_count must be a positive integer")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
        raise ValueError("shard_index must be within shard_count")
    failures, written = [], 0
    with HttpClient(**settings) as client:
        print(f"HTTP pacing: per-session interval={client.interval:g}s; "
              f"global interval={client.global_interval:g}s")
        diagnostics.records = client.records
        print(f"Shard {index + 1}/{count} of {provider}: {first} through {last}")
        target = first
        while target <= last:
            args._diagnostic_target = target.isoformat()
            client.records.clear()
            try:
                bundle = fetch_shard(provider, target, config["providers"][provider],
                                     client, index, count,
                                     progress=lambda message: print(message, flush=True))
            except Exception as exc:
                entry = diagnostics.failure(provider, target.isoformat(), exc,
                                            client.records)
                failures.append(target.isoformat())
                print(f"FAILED {target}: {entry['brief']}", file=sys.stderr)
            else:
                path = shard_bundle_path(args.shard_dir, provider, target, index)
                atomic_write(path, json_bytes(bundle, compact=True))
                written += 1
                print(f"SHARD {target}: {len(bundle['rows'])} pairs -> {path}")
            if target == last:
                break
            target += timedelta(days=1)
    status = 1 if failures else 0
    summary = diagnostics.finish("fetch-shard", provider, status,
                                 f"shard {index + 1}/{count}; bundles written: {written}")
    print(summary)
    _emit_summary(summary)
    return status


def _run_merge(config: dict, args: argparse.Namespace, diagnostics: Diagnostics,
               publisher: BranchPublisher | None, store: DataStore,
               first: date, last: date) -> int:
    provider = args.provider
    successes, failures = [], []
    print(f"Merging shards for {provider}: {first} through {last}")
    target = first
    while target <= last:
        args._diagnostic_target = target.isoformat()
        try:
            bundles = load_shard_bundles(args.shard_dir, provider, target)
            result = merge_shards(provider, target, bundles)
        except Exception as exc:
            entry = diagnostics.failure(provider, target.isoformat(), exc, [])
            failures.append(target.isoformat())
            print(f"FAILED {target}: {entry['brief']}", file=sys.stderr)
        else:
            # A storage failure aborts data-branch publication entirely.
            store.save_day(result)
            successes.append(target.isoformat())
            diagnostics.success(result)
            print(f"SAVED {target}: {result.metadata['pair_count']} pairs")
        if target == last:
            break
        target += timedelta(days=1)
    if publisher is not None and successes:
        publisher.publish(f"Fetch {provider}: {first} through {last}")
    status = 1 if failures else 0
    summary = diagnostics.finish("merge-shards", provider, status)
    print(summary)
    _emit_summary(summary)
    return status


def execute(args: argparse.Namespace, diagnostics: Diagnostics | None = None) -> int:
    diagnostics = diagnostics or Diagnostics(getattr(args, "diagnostics_dir", Path("diagnostics")),
                                             getattr(args, "data_dir", None))
    if args.command == "export-currency-api":
        DataStore(args.data_dir, stablecoins=load_stablecoins(args.stablecoins_config)).export_currency_api(args.provider)
        root = args.data_dir / PROVIDER_PATHS[args.provider]
        print(f"Exported {args.provider} currency-api files to "
              f"{root / 'direct/v1'} and {root / 'reverse/v1'}")
        return 0
    config = load_config(args.config)
    first, last = dates(args.start_date, args.end_date)
    if args.command == "preflight":
        settings = http_settings(config, args)
        # This is an availability probe, not an exhaustive recovery attempt.
        settings["timeout"] = min(settings.get("timeout", 30), 10)
        settings["retries"] = min(settings.get("retries", 3), 1)
        with HttpClient(**settings) as client:
            diagnostics.records = client.records
            target = first
            while target <= last:
                args._diagnostic_target = target.isoformat()
                print(f"Checking Mastercard rate date {target}", flush=True)
                preflight_mastercard(target, config["providers"]["mastercard"], client)
                print(f"READY {target}: 3 sample pairs have matching rate dates", flush=True)
                if target == last:
                    break
                target += timedelta(days=1)
        return 0
    if args.command == "fetch-shard":
        # Shard jobs never write published data; they only stage bundle files.
        return _run_shard(config, args, diagnostics, first, last,
                          http_settings(config, args))
    publisher = None
    if args.publish:
        publisher = BranchPublisher(Path.cwd(), args.data_dir, args.remote, args.branch)
        publisher.prepare()
    store = DataStore(args.data_dir, stablecoins=load_stablecoins(args.stablecoins_config))
    if args.command == "merge-shards":
        return _run_merge(config, args, diagnostics, publisher, store, first, last)
    assert first is not None and last is not None
    successes, failures = [], []
    settings = http_settings(config, args)
    with HttpClient(**settings) as client:
        print(f"HTTP pacing: per-session interval={client.interval:g}s; "
              f"global interval={client.global_interval:g}s")
        diagnostics.records = client.records
        catalog = prepare_catalog(args.provider, config["providers"][args.provider], client)
        pair_count = catalog.pair_count if catalog is not None else (2 if args.provider == "jcb" else 1)
        count = (last - first).days + 1
        if catalog is not None:
            print(f"Official catalog: {len(catalog.transaction)} transaction / "
                  f"{len(catalog.billing)} billing currencies; {SHARD_COUNT} concurrent shards")
        print(f"Provider {args.provider}: {first} through {last}; {count * pair_count} base requests")
        target = first
        while target <= last:
            args._diagnostic_target = target.isoformat()
            client.records.clear()
            try:
                result = fetch_day(args.provider, target, catalog, config["providers"][args.provider], client)
            except JcbUnavailableError as exc:
                entry = diagnostics.failure(args.provider, target.isoformat(), exc, client.records,
                                            status=exc.availability)
                print(f"SKIPPED {target} ({exc.availability}): {entry['brief']}")
            except Exception as exc:
                entry = diagnostics.failure(args.provider, target.isoformat(), exc, client.records)
                failures.append(target.isoformat())
                print(f"FAILED {target}: {entry['brief']}", file=sys.stderr)
            else:
                # A storage failure aborts data-branch publication entirely.
                results = result if isinstance(result, tuple) else (result,)
                for item in results:
                    store.save_day(item)
                    diagnostics.success(item)
                successes.append(target.isoformat())
                print(f"SAVED {target}: {', '.join(item.provider for item in results)}")
            if target == last:
                break
            target += timedelta(days=1)
    if publisher is not None and successes:
        publisher.publish(f"Fetch {args.provider}: {first} through {last}")
    status = 1 if failures else 0
    summary = diagnostics.finish(args.command, args.provider, status)
    print(summary)
    _emit_summary(summary)
    return status


def main(argv: list[str] | None = None) -> int:
    args = None
    diagnostics = None
    try:
        args = parser().parse_args(argv)
        diagnostics = Diagnostics(getattr(args, "diagnostics_dir", Path("diagnostics")),
                                  getattr(args, "data_dir", None))
        return execute(args, diagnostics)
    except (Exception, KeyboardInterrupt) as exc:
        diagnostics = diagnostics or Diagnostics(Path("diagnostics"))
        entry = diagnostics.failure(getattr(args, "provider", ""),
                                    getattr(args, "_diagnostic_target", None), exc,
                                    diagnostics.records, fatal=True)
        print(f"ERROR: {entry['brief']}", file=sys.stderr)
        summary = diagnostics.finish(getattr(args, "command", "task"),
                                     getattr(args, "provider", ""), 1)
        _emit_summary(summary)
        return 1
