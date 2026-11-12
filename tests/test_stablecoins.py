import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest

from exchange_rates.cli import main
from exchange_rates.currency_api import build_currency_api
from exchange_rates.stablecoins import load_stablecoins, validate_stablecoins
from exchange_rates.storage import DataStore
from exchange_rates.models import json_bytes
from test_currency_api import decode, mastercard


class StablecoinTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write_config(self, coins):
        path = self.root / "stablecoins.json"
        path.write_text(json.dumps({
            "schema_version": 1, "policy": "fixed_usd_parity", "coins": coins,
        }))
        return path

    def test_default_registry_covers_major_assets_without_claim_tokens_or_fiat_collision(self):
        coins = load_stablecoins()
        expected = {
            "usdt", "usdc", "dai", "usds", "usde", "usd1", "usd0", "usdtb",
            "rlusd", "pyusd", "fdusd", "tusd", "usdp", "gusd", "usdg",
            "gho", "crvusd", "frxusd", "lusd", "bold", "usdd", "ausd",
            "usdt0", "usdc.e", "usdbc",
        }
        self.assertTrue(expected <= coins.keys())
        self.assertGreaterEqual(len(coins), 100)
        for code in ("usd", "gyd", "eurc", "paxg", "xaut", "susde", "susds", "sdai",
                     "ust", "ustc", "husd", "frax", "busd", "fei", "susd", "djed"):
            self.assertNotIn(code, coins)
        self.assertIn("gyroscope-gyd", coins)
        self.assertEqual(len(coins), len(set(coins)))

    def test_every_enabled_asset_has_usd_parity_and_directional_quotes(self):
        coins = load_stablecoins()
        files = build_currency_api(mastercard(rate=Decimal("0.14")))
        for direction in ("direct", "reverse"):
            prefix = f"mastercard/{direction}"
            usd = decode(files[f"{prefix}/v1/currencies/usd.json"])["usd"]
            cny = decode(files[f"{prefix}/v1/currencies/cny.json"])["cny"]
            meta = decode(files[f"{prefix}/metadata.json"])
            self.assertEqual(meta["stablecoins"], {code: 1 for code in coins})
            for coin in coins:
                self.assertEqual(cny[coin], cny["usd"])
                self.assertEqual(usd[coin], 1)
                payload = decode(files[f"{prefix}/v1/currencies/{coin}.json"])
                self.assertEqual(payload[coin], usd)
                self.assertEqual(payload[coin][coin], 1)
                self.assertEqual(decode(files[f"{prefix}/v1/currencies/{coin}.min.json"]), payload)

    def test_configuration_can_enable_disable_and_add_assets(self):
        path = self.write_config({
            "USDT": {"name": "Tether"}, "custom-usd": {"name": "Custom Dollar"},
            "busd": {"name": "Binance USD", "enabled": True},
            "dai": {"name": "Dai", "enabled": False},
        })
        coins = load_stablecoins(path)
        self.assertEqual(coins, {"usdt": "Tether", "custom-usd": "Custom Dollar", "busd": "Binance USD"})
        files = build_currency_api(mastercard(), coins)
        cny = decode(files["mastercard/direct/v1/currencies/cny.json"])["cny"]
        self.assertEqual(cny["custom-usd"], cny["usd"])
        self.assertNotIn("dai", cny)

    def test_gyd_fiat_and_gyroscope_stablecoin_remain_different_units(self):
        source = mastercard()
        data = decode(source.normalized)
        data["exchangeRateJson"].extend([
            {"transCur": "USD", "baseCur": "GYD", "rateData": Decimal("209")},
            {"transCur": "GYD", "baseCur": "USD", "rateData": Decimal("0.0048")},
        ])
        source.normalized = json_bytes(data)
        files = build_currency_api(source)
        usd = decode(files["mastercard/direct/v1/currencies/usd.json"])["usd"]
        self.assertEqual(usd["gyd"], 209)
        self.assertEqual(usd["gyroscope-gyd"], 1)
        stable = decode(files["mastercard/direct/v1/currencies/gyroscope-gyd.json"])["gyroscope-gyd"]
        self.assertEqual(stable["gyd"], 209)
        fiat = decode(files["mastercard/direct/v1/currencies/gyd.json"])["gyd"]
        self.assertEqual(fiat["gyroscope-gyd"], Decimal("0.0048"))

    def test_invalid_registry_and_ambiguous_currency_keys_fail_before_export(self):
        for code in ("usd", "../evil", "a/b", "a..b", "date", "usd.min", "x" * 65):
            with self.subTest(code=code), self.assertRaises(ValueError):
                load_stablecoins(self.write_config({code: {"name": "Invalid"}}))
        for entry in ({"name": ""}, {"name": 1}, {"name": "USD", "enabled": "false"}, "USD"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                load_stablecoins(self.write_config({"custom": entry}))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_stablecoins({"USDT": "Tether", "usdt": "Duplicate"})
        with self.assertRaisesRegex(ValueError, "conflicts"):
            build_currency_api(mastercard(), {"cny": "Not a stablecoin"})
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":1,"policy":"fixed_usd_parity","coins":'
                        '{"usdt":{"name":"Tether"},"usdt":{"name":"Duplicate"}}}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_stablecoins(path)

    def test_disabled_assets_are_removed_on_offline_cli_reexport(self):
        data_dir = self.root / "data"
        store = DataStore(data_dir, stablecoins={"usdt": "Tether", "custom-usd": "Custom Dollar"})
        store.save_day(mastercard())
        store.save_day(mastercard(provider="visa"))
        path = self.write_config({"usdt": {"name": "Tether"}})
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["export-currency-api", "--provider", "mastercard",
                           "--data-dir", str(data_dir), "--stablecoins-config", str(path)])
        self.assertEqual(status, 0)
        for direction in ("direct", "reverse"):
            root = data_dir / "mastercard" / direction / "v1"
            self.assertFalse((root / "currencies/custom-usd.json").exists())
            self.assertFalse((root / "currencies/custom-usd.min.json").exists())
            self.assertNotIn("custom-usd", decode((root / "currencies.json").read_bytes()))
            self.assertNotIn("custom-usd", decode((root / "currencies/cny.json").read_bytes())["cny"])
            self.assertTrue((data_dir / "visa" / direction / "v1/currencies/custom-usd.json").exists())


if __name__ == "__main__":
    unittest.main()
