import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from exchange_rates.models import DayResult, json_bytes
from exchange_rates.storage import DataStore, StorageError, sha256


def result(day, provider="visa", rate=7):
    return DayResult(provider, day, json_bytes({"exchangeRateJson": [
        {"transCur": "USD", "baseCur": "CNY", "rateData": rate},
    ]}), json_bytes({"body_text": str(rate)}), {"fetched_at_utc": "2026-10-03T00:00:00Z"})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.store = DataStore(self.root)


    def test_same_day_overwrite_and_older_day_does_not_replace_latest(self):
        self.store.save_day(result(date(2026, 9, 30), rate=7))
        self.store.save_day(result(date(2026, 9, 30), rate=8))
        self.store.save_day(result(date(2026, 9, 20), rate=6))
        current = json.loads((self.root / "latest/visa.json").read_bytes())
        self.assertEqual(current["exchangeRateJson"][0]["rateData"], 8)
        self.assertEqual(json.loads((self.root / "metadata/latest/visa.json").read_bytes())["requested_date"], "2026-09-30")
        self.assertEqual(json.loads((self.root / "raw/latest/visa.json").read_bytes())["body_text"], "8")


if __name__ == "__main__":
    unittest.main()
