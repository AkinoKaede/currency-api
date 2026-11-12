import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from exchange_rates.models import DayResult, json_bytes
from exchange_rates.storage import DataStore, StorageError, member_info


def daily(day, provider="visa", value=7, sharded=True):
    parts = {}
    if sharded:
        for number in range(1, 10):
            parts[f"shard-{number:02d}.json"] = json_bytes({
                "provider": provider, "requested_date": day.isoformat(), "shard_index": number,
                "transaction_currencies": ["USD"] if number == 1 else [],
                "requests": [{"body_text": str(value)}] if number == 1 else [],
            })
        raw = json_bytes({
            "format": "exchange-rates-raw-shards-v1", "provider": provider,
            "requested_date": day.isoformat(), "catalog_requests": [],
            "parts": [{"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                      for name, body in sorted(parts.items())],
        })
    else:
        raw = json_bytes({"body_text": str(value)})
    return DayResult(provider, day, json_bytes({
        "rate": value, "curDate": day.isoformat(),
        "exchangeRateJson": [{"transCur": "USD", "baseCur": "CNY", "rateData": value}],
    }), raw, {"fetched_at_utc": "fixture"}, parts)


class ShardedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "data"
        self.store = DataStore(self.root)


    def latest(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for role in ("latest", "raw/latest", "metadata/latest")
                for path in (self.root / role).rglob("*") if path.is_file()}

    def test_nine_parts_daily_latest_and_legacy_unionpay(self):
        day = date(2026, 9, 30)
        result = daily(day)
        self.store.save_day(result)
        index = json.loads((self.root / "raw/history/2026/09/visa/2026-09-30.json").read_bytes())
        latest = json.loads((self.root / "raw/latest/visa.json").read_bytes())
        self.assertEqual(index["parts_directory"], "2026-09-30.parts")
        self.assertEqual(latest["parts_directory"], "visa.parts")
        self.assertEqual(set(result.raw_parts), {path.name for path in (self.root / "raw/latest/visa.parts").iterdir()})
        (self.root / "raw/latest/visa.parts/shard-10.json").write_bytes(b"orphan")
        self.store.save_day(result)
        self.assertFalse((self.root / "raw/latest/visa.parts/shard-10.json").exists())
        legacy = daily(day, "unionpay", sharded=False)
        self.store.save_day(legacy)
        self.assertEqual((self.root / "raw/latest/unionpay.json").read_bytes(), legacy.raw)

    def test_bad_raw_result_is_rejected_before_writes(self):
        day = date(2026, 9, 30)
        self.store.save_day(daily(day))
        before = self.latest()
        bad = daily(day, value=8)
        index = json.loads(bad.raw)
        index["parts"][0]["sha256"] = "0" * 64
        bad.raw = json_bytes(index)
        with self.assertRaises(StorageError):
            self.store.save_day(bad)
        self.assertEqual(self.latest(), before)
        bad = daily(day, value=8)
        del bad.raw_parts["shard-09.json"]
        with self.assertRaises(StorageError):
            self.store.save_day(bad)
        self.assertEqual(self.latest(), before)


    def test_paths_reject_cross_period_and_non_raw_parts(self):
        for name in ("raw/history/2026/09/visa/2026-09-30.parts/../shard-01.json",
                     "raw/history/2026/09/visa/2026-08-30.parts/shard-01.json",
                     "history/2026/09/visa/2026-09-30.parts/shard-01.json",
                     "/raw/history/2026/09/visa/2026-09-30.parts/shard-01.json",
                     "raw/history/2026/09/visa/2026-09-31.parts/shard-01.json"):
            with self.subTest(name=name), self.assertRaises(StorageError):
                member_info(name, "2026-09")


if __name__ == "__main__":
    unittest.main()
