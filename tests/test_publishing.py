import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from exchange_rates.models import DayResult, json_bytes
from exchange_rates.publishing import BranchPublisher, PublishError
from exchange_rates.storage import DataStore


def run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.source.mkdir()
        run(self.root, "init", "--bare", str(self.remote))
        run(self.source, "init", "--initial-branch=main")
        run(self.source, "config", "user.name", "Test")
        run(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "source.txt").write_text("source only\n")
        run(self.source, "add", ".")
        run(self.source, "commit", "-m", "Initial source")
        run(self.source, "remote", "add", "origin", str(self.remote))
        run(self.source, "push", "origin", "main")

    def publisher(self, name):
        publisher = BranchPublisher(self.source, self.root / name)
        publisher.prepare()
        return publisher

    def save(self, publisher, day, rate=7):
        store = DataStore(publisher.data_dir)
        store.save_day(DayResult("visa", day, json_bytes({"exchangeRateJson": [
            {"transCur": "USD", "baseCur": "CNY", "rateData": rate},
        ]}), json_bytes({"raw": rate}), {}))
        return store

    def test_daily_and_compatible_files_publish_without_rewriting_history(self):
        first = self.publisher("first")
        self.save(first, date(2025, 12, 30))
        self.assertTrue(first.publish("First day"))
        self.assertEqual(run(self.remote, "rev-list", "--count", "data"), "1")
        self.assertNotIn("source.txt", run(self.remote, "ls-tree", "--name-only", "data"))
        second = self.publisher("second")
        self.save(second, date(2026, 1, 1))
        second.publish("Second day")
        self.assertEqual(run(self.remote, "rev-list", "--count", "data"), "2")
        third = self.publisher("third")
        self.assertTrue((third.data_dir / "history/2025/12/visa/2025-12-30.json").exists())
        self.assertTrue((third.data_dir / "visa/direct/v1/currencies/usdt.json").exists())
        self.assertTrue((third.data_dir / "visa/reverse/v1/currencies/cny.json").exists())
        self.assertFalse(third.publish("No changes"))

    def test_concurrent_remote_update_is_not_overwritten(self):
        first = self.publisher("first")
        self.save(first, date(2026, 9, 30))
        first.publish("Initial")
        stale = self.publisher("stale")
        newer = self.publisher("newer")
        self.save(newer, date(2026, 10, 1))
        newer.publish("New day")
        expected = run(self.remote, "rev-parse", "data")
        self.save(stale, date(2026, 9, 30), rate=8)
        with self.assertRaises(PublishError):
            stale.publish("Stale update")
        self.assertEqual(run(self.remote, "rev-parse", "data"), expected)

    def test_existing_local_data_is_never_cleared(self):
        directory = self.root / "existing"
        directory.mkdir()
        marker = directory / "keep.txt"
        marker.write_text("keep")
        with self.assertRaises(PublishError):
            BranchPublisher(self.source, directory)
        self.assertEqual(marker.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
