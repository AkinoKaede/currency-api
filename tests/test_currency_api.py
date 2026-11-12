import contextlib
from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import simplejson

from exchange_rates.cli import main
from exchange_rates.currency_api import PROVIDER_PATHS, build_currency_api
from exchange_rates.models import DayResult, json_bytes
from exchange_rates.storage import DataStore
from exchange_rates.stablecoins import load_stablecoins


def mastercard(day=date(2026, 11, 12), rate=Decimal("0.14001234567890123456789"),
               provider="mastercard"):
    # Intentionally non-reciprocal quotes expose accidental direction inversion.
    rows = [
        {"transCur": "CNY", "baseCur": "USD", "rateData": rate},
        {"transCur": "USD", "baseCur": "CNY", "rateData": Decimal("7.2")},
        {"transCur": "EUR", "baseCur": "USD", "rateData": Decimal("1.08")},
        {"transCur": "USD", "baseCur": "EUR", "rateData": Decimal("0.91")},
    ]
    return DayResult(provider, day, json_bytes({
        "curDate": day.isoformat(), "exchangeRateJson": rows,
    }), b"{}", {"source_urls": [f"https://example.test/{provider}"],
               "fetched_at_utc": "2026-11-12T01:00:00Z"})


def decode(body):
    return simplejson.loads(body, use_decimal=True)


def jcb(column="buy", quote="3.6727"):
    exact = 1 / Fraction(quote)
    with localcontext() as context:
        context.prec = 100
        context.rounding = ROUND_DOWN
        rate = Decimal(exact.numerator) / Decimal(exact.denominator)
    return DayResult(f"jcb-usd-{column}", date(2026, 11, 12), json_bytes({
        "curDate": "2026-11-12", "exchangeRateJson": [{
            "transCur": "CNY", "baseCur": "USD", "rateData": rate,
            "rateNumerator": exact.numerator, "rateDenominator": exact.denominator,
        }],
    }), b"{}", {"quote_column": column.title()})


class CurrencyApiTests(unittest.TestCase):
    def test_jcb_columns_are_separate_and_reverse_uses_exact_fraction(self):
        for column, value in (("buy", "3.6727"), ("mid", "3.7"), ("sell", "3.8")):
            source = jcb(column, value)
            files = build_currency_api(source)
            prefix = f"jcb/usd-{column}"
            direct = decode(files[f"{prefix}/direct/v1/currencies/cny.json"])
            self.assertEqual(direct["cny"]["usdt"],
                             decode(source.normalized)["exchangeRateJson"][0]["rateData"])
            reverse = decode(files[f"{prefix}/reverse/v1/currencies/usdt.json"])
            self.assertEqual(reverse["usdt"]["cny"], Decimal(value))
            self.assertNotIn("usdt", decode(files[f"{prefix}/reverse/v1/currencies/cny.json"])["cny"])
            metadata = decode(files[f"{prefix}/reverse/metadata.json"])
            self.assertEqual(metadata["quote_column"], column.title())
            self.assertEqual(metadata["reciprocal_source"], "exact rateNumerator/rateDenominator")

    def test_jcb_inconsistent_fraction_is_rejected(self):
        for updates in ({"rateNumerator": 0}, {"rateDenominator": True},
                        {"rateNumerator": 1}, {"baseCur": "JPY"}):
            source = jcb()
            data = decode(source.normalized)
            data["exchangeRateJson"][0].update(updates)
            source.normalized = json_bytes(data)
            with self.assertRaises(ValueError):
                build_currency_api(source)

    def test_jcb_jpy_does_not_invent_cny_usd_cross_rate(self):
        source = DayResult("jcb-jpy", date(2026, 11, 12), json_bytes({
            "exchangeRateJson": [
                {"transCur": "CNY", "baseCur": "JPY", "rateData": 20},
                {"transCur": "USD", "baseCur": "JPY", "rateData": 150},
            ],
        }), b"{}", {"derived": True})
        files = build_currency_api(source)
        self.assertEqual(decode(files["jcb/jpy/direct/v1/currencies/cny.json"])["cny"]["jpy"], 20)
        self.assertNotIn("usdt", decode(files["jcb/jpy/direct/v1/currencies/cny.json"])["cny"])
        self.assertIn("usdt", decode(files["jcb/jpy/reverse/v1/currencies/jpy.json"])["jpy"])
        self.assertTrue(decode(files["jcb/jpy/direct/metadata.json"])["source_derived"])

    def test_all_three_networks_have_independent_namespaces_and_provenance(self):
        for provider in ("mastercard", "visa", "unionpay"):
            with self.subTest(provider=provider):
                result = mastercard(rate=Decimal("0.14"), provider=provider)
                files = build_currency_api(result)
                self.assertTrue(all(name.startswith(provider + "/") for name in files))
                for direction in ("direct", "reverse"):
                    payload = decode(files[f"{provider}/{direction}/v1/currencies/cny.json"])
                    self.assertEqual(payload["date"], "2026-11-12")
                    self.assertGreater(payload["cny"]["usdt"], 0)
                    meta = decode(files[f"{provider}/{direction}/metadata.json"])
                    self.assertEqual(meta["provider"], provider)
                    self.assertEqual(meta["source_file"], f"latest/{provider}.json")
                    self.assertEqual(meta["source_urls"], [f"https://example.test/{provider}"])

    def test_gmpay_multiplies_cny_amount_by_usdt_quote_without_inversion(self):
        files = build_currency_api(mastercard(rate=Decimal("0.14")))
        payload = decode(files["mastercard/direct/v1/currencies/cny.json"])
        self.assertEqual(payload["date"], "2026-11-12")
        self.assertEqual(payload["cny"], {
            "cny": 1, "usd": Decimal("0.14"),
            **{code: Decimal("0.14") for code in load_stablecoins()},
        })
        self.assertEqual(Decimal("100") * payload["cny"]["usdt"], Decimal("14"))
        # There is no original CNY/EUR quote, so no fabricated cross rate.
        self.assertNotIn("eur", payload["cny"])

    def test_usd_aliases_use_original_reverse_quote_and_exact_parity(self):
        files = build_currency_api(mastercard())
        usd = decode(files["mastercard/direct/v1/currencies/usd.json"])["usd"]
        self.assertEqual(usd["cny"], Decimal("7.2"))
        self.assertEqual(usd["eur"], Decimal("0.91"))
        for coin in ("usd", "usdt", "usdc", "dai"):
            quotes = decode(files[f"mastercard/direct/v1/currencies/{coin}.json"])[coin]
            self.assertEqual(quotes, usd)
            for other in ("usd", "usdt", "usdc", "dai"):
                self.assertEqual(quotes[other], 1)

    def test_reverse_view_uses_opposite_quote_but_keeps_gmpay_units(self):
        files = build_currency_api(mastercard(rate=Decimal("0.14")))
        direct = decode(files["mastercard/direct/v1/currencies/cny.json"])["cny"]["usdt"]
        reverse = decode(files["mastercard/reverse/v1/currencies/cny.json"])["cny"]["usdt"]
        # Reverse CNY/USDT uses 1 / 7.2, not 1 / 0.14.
        self.assertEqual(reverse, Decimal("0.13" + "8" * 98))
        self.assertNotEqual(direct, reverse)
        with localcontext() as context:
            context.prec = 110
            self.assertLess(reverse * Decimal("7.2"), 1)
            self.assertLess(1 - reverse * Decimal("7.2"), Decimal("1e-99"))
        for coin in ("usd", "usdt", "usdc", "dai"):
            quotes = decode(files[f"mastercard/reverse/v1/currencies/{coin}.json"])[coin]
            self.assertEqual(quotes["cny"], Decimal("7." + "142857" * 16 + "142"))
            for other in ("usd", "usdt", "usdc", "dai"):
                self.assertEqual(quotes[other], 1)
        meta = decode(files["mastercard/reverse/metadata.json"])
        self.assertEqual(meta["rate_formula"], "1 / mastercard(output -> input)")
        self.assertIn("100 significant digits", meta["precision_policy"])

    def test_one_way_quotes_are_not_inverted_inside_direct_directory(self):
        source = mastercard()
        data = decode(source.normalized)
        data["exchangeRateJson"] = [data["exchangeRateJson"][0]]
        source.normalized = json_bytes(data)
        files = build_currency_api(source)
        self.assertIn("usdt", decode(files["mastercard/direct/v1/currencies/cny.json"])["cny"])
        self.assertNotIn("cny", decode(files["mastercard/direct/v1/currencies/usdt.json"])["usdt"])
        self.assertNotIn("usdt", decode(files["mastercard/reverse/v1/currencies/cny.json"])["cny"])
        self.assertIn("cny", decode(files["mastercard/reverse/v1/currencies/usdt.json"])["usdt"])

    def test_precision_minified_files_catalog_and_provenance(self):
        source = mastercard()
        files = build_currency_api(source)
        cny = decode(files["mastercard/direct/v1/currencies/cny.json"])["cny"]
        self.assertEqual(cny["usdt"], Decimal("0.14001234567890123456789"))
        for name, body in files.items():
            if "/v1/" in name and not name.endswith(".min.json"):
                self.assertEqual(decode(body), decode(files[name[:-5] + ".min.json"]))
        for direction in ("direct", "reverse"):
            self.assertEqual(set(decode(files[f"mastercard/{direction}/v1/currencies.json"])),
                             {"usd", "cny", "eur"} | set(load_stablecoins()))
        meta = decode(files["mastercard/direct/metadata.json"])
        self.assertEqual(meta["source_sha256"], hashlib.sha256(source.normalized).hexdigest())
        self.assertEqual(meta["additional_markup_percent"], 0)
        self.assertEqual(meta["source_urls"], source.metadata["source_urls"])

    def test_invalid_source_cannot_produce_a_chargeable_rate(self):
        original = decode(mastercard().normalized)
        invalid = [
            {}, {"exchangeRateJson": []},
            {**original, "curDate": "2026-11-11"},
            {**original, "exchangeRateJson": original["exchangeRateJson"] * 2},
        ]
        for field, value in (
            ("rateData", 0), ("rateData", -1), ("rateData", True),
            ("rateData", "0.14"), ("rateData", None),
            ("transCur", "../USD"), ("baseCur", "USDT"),
            ("baseCur", "DAI"),
        ):
            invalid.append({**original, "exchangeRateJson": [
                {**original["exchangeRateJson"][0], field: value},
            ]})
        invalid.extend([
            {**original, "exchangeRateJson": [
                {"transCur": "CNY", "baseCur": "EUR", "rateData": 1},
            ]},
            {**original, "exchangeRateJson": [
                {"transCur": "USD", "baseCur": "USD", "rateData": 2},
            ]},
        ])
        for payload in invalid:
            with self.subTest(payload=payload):
                source = mastercard()
                source.normalized = json_bytes(payload)
                with self.assertRaises(ValueError):
                    build_currency_api(source)
        for body in (b'{"exchangeRateJson": [NaN]}', b'{"exchangeRateJson": [Infinity]}'):
            source = mastercard()
            source.normalized = body
            with self.assertRaises(ValueError):
                build_currency_api(source)
        source = mastercard()
        source.provider = "unsupported"
        with self.assertRaises(ValueError):
            build_currency_api(source)


class CurrencyApiStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "data"
        self.store = DataStore(self.root)

    def test_jcb_columns_save_and_rebuild_independently(self):
        for column, value in (("buy", "3.6727"), ("mid", "3.7"), ("sell", "3.8")):
            self.store.save_day(jcb(column, value))
        for column, value in (("buy", "3.6727"), ("mid", "3.7"), ("sell", "3.8")):
            provider = f"jcb-usd-{column}"
            self.store.export_currency_api(provider)
            payload = decode((self.root / PROVIDER_PATHS[provider] / "reverse/v1/currencies/usdt.json").read_bytes())
            self.assertEqual(payload["usdt"]["cny"], Decimal(value))

    def test_saving_mastercard_exports_and_backfill_does_not_roll_back_view(self):
        current = mastercard()
        self.store.save_day(current)
        expected = build_currency_api(current)
        self.store.save_day(mastercard(date(2026, 11, 10), Decimal("0.13")))
        for name, body in expected.items():
            self.assertEqual((self.root / name).read_bytes(), body)
        self.assertEqual((self.root / "latest/mastercard.json").read_bytes(), current.normalized)
        self.assertTrue((self.root / "history/2026/11/mastercard/2026-11-10.json").exists())
        # Same-day corrections must replace the view.
        self.store.save_day(mastercard(rate=Decimal("0.15")))
        self.assertEqual(decode((self.root / "mastercard/direct/v1/currencies/cny.json").read_bytes())["cny"]["usdt"],
                         Decimal("0.15"))
        # A change to CNY/USD affects reverse USD/CNY, not reverse CNY/USD.
        reverse = decode((self.root / "mastercard/reverse/v1/currencies/usdt.json").read_bytes())
        self.assertEqual(reverse["usdt"]["cny"], Decimal("6." + "6" * 99))

    def test_other_providers_do_not_replace_mastercard_view(self):
        self.store.save_day(mastercard())
        before = {name: (self.root / name).read_bytes() for name in build_currency_api(mastercard())}
        other = mastercard(date(2026, 11, 13), Decimal("0.9"))
        other.provider = "visa"
        self.store.save_day(other)
        for name, body in before.items():
            self.assertEqual((self.root / name).read_bytes(), body)
        visa = decode((self.root / "visa/direct/v1/currencies/cny.json").read_bytes())
        self.assertEqual(visa["cny"]["usdt"], Decimal("0.9"))
        self.assertEqual(visa["date"], "2026-11-13")
        self.store.save_day(mastercard(date(2026, 11, 9), Decimal("0.12"), "unionpay"))
        unionpay = decode((self.root / "unionpay/direct/v1/currencies/cny.json").read_bytes())
        self.assertEqual(unionpay["cny"]["usdt"], Decimal("0.12"))
        self.assertEqual(unionpay["date"], "2026-11-09")
        self.store.save_day(mastercard(date(2026, 11, 8), Decimal("0.11"), "visa"))
        self.assertEqual(decode((self.root / "visa/direct/v1/currencies/cny.json").read_bytes()), visa)
        for name, body in before.items():
            self.assertEqual((self.root / name).read_bytes(), body)

    def test_validation_failure_leaves_all_existing_data_untouched(self):
        self.store.save_day(mastercard())
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        bad = mastercard(date(2026, 11, 13), Decimal("-1"))
        with self.assertRaises(ValueError):
            self.store.save_day(bad)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                 for p in self.root.rglob("*") if p.is_file()})

    def test_removed_currency_files_are_retired(self):
        self.store.save_day(mastercard())
        self.store.save_day(mastercard(provider="visa"))
        updated = mastercard(date(2026, 11, 13))
        payload = decode(updated.normalized)
        payload["exchangeRateJson"] = payload["exchangeRateJson"][:2]
        updated.normalized = json_bytes(payload)
        self.store.save_day(updated)
        for direction in ("direct", "reverse"):
            self.assertFalse((self.root / "mastercard" / direction / "v1/currencies/eur.json").exists())
            self.assertFalse((self.root / "mastercard" / direction / "v1/currencies/eur.min.json").exists())
            self.assertNotIn("eur", decode((self.root / "mastercard" / direction / "v1/currencies.json").read_bytes()))
            self.assertTrue((self.root / "visa" / direction / "v1/currencies/eur.json").exists())

    def test_existing_data_can_be_exported_offline_with_cli(self):
        (self.root / "latest").mkdir()
        (self.root / "metadata/latest").mkdir(parents=True)
        for provider in ("mastercard", "visa", "unionpay"):
            source = mastercard(provider=provider)
            (self.root / "latest" / f"{provider}.json").write_bytes(source.normalized)
            (self.root / "metadata/latest" / f"{provider}.json").write_bytes(json_bytes({
                **source.metadata, "provider": provider,
                "requested_date": source.requested_date.isoformat(),
            }))
            with patch("exchange_rates.cli.HttpClient") as client, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["export-currency-api", "--provider", provider,
                                       "--data-dir", str(self.root)]), 0)
            client.assert_not_called()
            for name, body in build_currency_api(source).items():
                self.assertEqual((self.root / name).read_bytes(), body)



if __name__ == "__main__":
    unittest.main()
