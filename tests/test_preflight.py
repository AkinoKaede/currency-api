import contextlib
import io
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from exchange_rates.catalog import CurrencyCatalog
from exchange_rates.cli import main
from exchange_rates.http import HttpError
from exchange_rates.providers import (
    DateMismatchError, ProviderError, fetch_shard, preflight_mastercard,
)
from tests.test_cli import FakeClient as CliClient
from tests.test_providers import DAY, FakeClient, call_pair


class PreflightTests(unittest.TestCase):
    def client(self, fx_date=DAY.isoformat()):
        def mutate(data):
            if fx_date is not None:
                data["data"]["fxDate"] = fx_date
        return FakeClient("mastercard", mutate=mutate)

    def test_ready_date_checks_three_directional_quotes_without_refresh(self):
        client = self.client()
        preflight_mastercard(DAY, {}, client)
        self.assertEqual([call_pair("mastercard", params) for _, params, _ in client.calls],
                         [("CNY", "USD"), ("USD", "CNY"), ("EUR", "USD")])
        self.assertTrue(all("_" not in params for _, params, _ in client.calls))

    def test_stale_date_stops_on_first_quote_without_full_matrix_or_recovery(self):
        client = self.client((DAY - timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(DateMismatchError, "Full collection was not started"):
            preflight_mastercard(DAY, {"recovery_rounds": 2}, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["exchange_date"], DAY.isoformat())
        self.assertEqual(client.forks, [])

    def test_each_sample_must_have_the_requested_date(self):
        client = self.client()
        original = client.mutate
        def second_is_stale(data):
            original(data)
            if len(client.calls) == 2:
                data["data"]["fxDate"] = (DAY - timedelta(days=1)).isoformat()
        client.mutate = second_is_stale
        with self.assertRaises(DateMismatchError):
            preflight_mastercard(DAY, {}, client)
        self.assertEqual(len(client.calls), 2)

    def test_missing_effective_date_is_not_readiness(self):
        client = self.client(None)
        with self.assertRaisesRegex(ProviderError, "explicit fxDate"):
            preflight_mastercard(DAY, {}, client)
        self.assertEqual(len(client.calls), 1)

    def test_transport_error_is_not_readiness(self):
        client = FakeClient("mastercard", fail_at=1)
        with self.assertRaises(HttpError):
            preflight_mastercard(DAY, {}, client)
        self.assertEqual(len(client.calls), 1)

    def test_cli_checks_each_date_and_never_opens_publisher_or_store(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("exchange_rates.cli.HttpClient", side_effect=CliClient) as transport, \
                patch("exchange_rates.cli.preflight_mastercard") as probe, \
                patch("exchange_rates.cli.BranchPublisher") as publisher, \
                patch("exchange_rates.cli.DataStore") as store, \
                contextlib.redirect_stdout(io.StringIO()):
            status = main(["preflight", "--provider", "mastercard",
                           "--start-date", DAY.isoformat(),
                           "--end-date", (DAY + timedelta(days=1)).isoformat(),
                           "--diagnostics-dir", temporary])
        self.assertEqual(status, 0)
        self.assertEqual([call.args[0] for call in probe.call_args_list],
                         [DAY, DAY + timedelta(days=1)])
        self.assertEqual(transport.call_args.kwargs["timeout"], 10)
        self.assertEqual(transport.call_args.kwargs["retries"], 1)
        publisher.assert_not_called()
        store.assert_not_called()

    def test_cli_stale_date_fails_with_diagnostics_without_trying_another_date(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("exchange_rates.cli.HttpClient", CliClient), \
                patch("exchange_rates.cli.preflight_mastercard",
                      side_effect=DateMismatchError("Requested date differs from fxDate")) as probe, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            status = main(["preflight", "--provider", "mastercard",
                           "--start-date", DAY.isoformat(),
                           "--end-date", (DAY + timedelta(days=1)).isoformat(),
                           "--diagnostics-dir", temporary])
            reports = list(Path(temporary).rglob("report.json"))
            self.assertTrue(reports)
            self.assertIn("DateMismatchError", reports[0].read_text())
        self.assertEqual(status, 1)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0], DAY)

    def test_progress_reports_errors_then_recovery_without_changing_results(self):
        catalog = CurrencyCatalog(("USD",), ("CNY", "EUR"))
        client = FakeClient("mastercard", fail_at=1)
        messages = []
        with patch("exchange_rates.providers.discover_currencies", return_value=catalog):
            bundle = fetch_shard("mastercard", DAY, {}, client, 0, 1,
                                 progress=messages.append)
        self.assertEqual(len(bundle["rows"]), 2)
        self.assertTrue(any("1/2 pairs complete" in text and "1 unresolved errors" in text
                            for text in messages))
        self.assertIn("2/2 pairs complete", messages[-1])
        self.assertIn("3 attempts", messages[-1])
        self.assertIn("0 unresolved errors", messages[-1])


if __name__ == "__main__":
    unittest.main()
