"""Independent GMPay / currency-api views of card-network reference rates."""

from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
from collections.abc import Mapping
import hashlib
import re

import simplejson

from .models import DayResult, json_bytes
from .stablecoins import load_stablecoins, validate_stablecoins


DIRECTIONS = ("direct", "reverse")
PROVIDER_PATHS = {
    "mastercard": "mastercard", "visa": "visa", "unionpay": "unionpay",
    "jcb-jpy": "jcb/jpy", "jcb-usd-buy": "jcb/usd-buy",
    "jcb-usd-mid": "jcb/usd-mid", "jcb-usd-sell": "jcb/usd-sell",
}
PROVIDERS = tuple(PROVIDER_PATHS)


def build_currency_api(result: DayResult, stablecoins: Mapping[str, str] | None = None) -> dict[str, bytes]:
    """Build direct and reciprocal-of-opposite views before writing any files."""
    if result.provider not in PROVIDERS or type(result.requested_date) is not date:
        raise ValueError("currency-api requires a supported, dated card-network result")
    stablecoins = load_stablecoins() if stablecoins is None else validate_stablecoins(stablecoins)
    payload = simplejson.loads(result.normalized, use_decimal=True, allow_nan=False)
    day = result.requested_date.isoformat()
    if not isinstance(payload, dict) or payload.get("curDate", day) != day:
        raise ValueError("Source date does not match the requested date")
    rows = payload.get("exchangeRateJson")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Source has no exchangeRateJson rows")

    rates: dict[str, dict[str, Decimal]] = {}
    currencies: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    exact_rates: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid currency row")
        spend, home = row.get("transCur"), row.get("baseCur")
        if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code)
               for code in (spend, home)):
            raise ValueError("Expected three-letter source currency codes")
        spend, home = spend.lower(), home.lower()
        if spend in stablecoins or home in stablecoins:
            raise ValueError("Source conflicts with a stablecoin alias")
        value = row.get("rateData")
        if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
            raise ValueError("Source rate must be a JSON number")
        value = Decimal(value)
        if not value.is_finite() or value <= 0:
            raise ValueError("Source rate must be finite and positive")
        if spend == home and value != 1:
            raise ValueError("Identity rate must equal one")
        if (spend, home) in pairs:
            raise ValueError(f"Duplicate source pair: {spend}/{home}")
        if result.provider.startswith("jcb-usd-"):
            numerator, denominator = row.get("rateNumerator"), row.get("rateDenominator")
            if home != "usd" or any(type(number) is not int or number <= 0
                                    for number in (numerator, denominator)):
                raise ValueError("JCB USD rows require a positive exact fraction and USD target")
            with localcontext() as context:
                context.prec = 100
                context.rounding = ROUND_DOWN
                if Decimal(numerator) / Decimal(denominator) != value:
                    raise ValueError("JCB rate disagrees with its exact fraction")
            exact_rates[spend, home] = numerator, denominator
        pairs.add((spend, home))
        currencies.update((spend, home))
        rates.setdefault(spend, {})[home] = value
    if "usd" not in currencies:
        raise ValueError("Source must contain USD")

    reversed_rates: dict[str, dict[str, Decimal]] = {}
    with localcontext() as context:
        context.prec = 100
        context.rounding = ROUND_DOWN
        for spend, quotes in rates.items():
            for home, value in quotes.items():
                if (spend, home) in exact_rates:
                    numerator, denominator = exact_rates[spend, home]
                    reverse = Decimal(denominator) / Decimal(numerator)
                else:
                    reverse = Decimal(1) / value
                reversed_rates.setdefault(home, {})[spend] = reverse

    files = {}
    for direction, quotes in (("direct", rates), ("reverse", reversed_rates)):
        files.update(_view(result, direction, quotes, currencies, stablecoins))
    return files


def _view(result: DayResult, direction: str, rates: dict[str, dict[str, Decimal]],
          currencies: set[str], stablecoins: Mapping[str, str]) -> dict[str, bytes]:
    day = result.requested_date.isoformat()
    prefix = f"{PROVIDER_PATHS[result.provider]}/{direction}"
    for currency in sorted(currencies):
        quotes = rates.setdefault(currency, {})
        quotes[currency] = Decimal(1)
        # transCur is the input currency, baseCur is the output currency.
        if "usd" in quotes:
            for stablecoin in stablecoins:
                quotes[stablecoin] = quotes["usd"]
    for stablecoin in stablecoins:
        rates[stablecoin] = dict(rates["usd"])

    labels = {currency: currency.upper() for currency in sorted(currencies)}
    labels.update(stablecoins)
    files = {}
    documents = {f"{prefix}/v1/currencies": labels}
    documents.update({
        f"{prefix}/v1/currencies/{currency}": {"date": day, currency: quotes}
        for currency, quotes in sorted(rates.items())
    })
    for name, document in documents.items():
        files[f"{name}.json"] = json_bytes(document)
        files[f"{name}.min.json"] = json_bytes(document, compact=True)
    files[f"{prefix}/metadata.json"] = json_bytes({
        "provider": result.provider,
        "direction": direction,
        "date": day,
        "source_file": f"latest/{result.provider}.json",
        "source_sha256": hashlib.sha256(result.normalized).hexdigest(),
        "source_urls": result.metadata.get("source_urls", []),
        "quote_column": result.metadata.get("quote_column"),
        "source_derived": result.metadata.get("derived", False),
        "fetched_at_utc": result.metadata.get("fetched_at_utc"),
        "rate_direction": "output currency per one input currency",
        "rate_formula": (f"{result.provider}(input -> output)" if direction == "direct"
                         else f"1 / {result.provider}(output -> input)"),
        "precision_policy": ("original decimal preserved" if direction == "direct"
                             else "reciprocal truncated to 100 significant digits with ROUND_DOWN"),
        "reciprocal_source": ("exact rateNumerator/rateDenominator"
                              if result.provider.startswith("jcb-usd-") else "rateData"),
        "stablecoin_policy": "fixed USD parity for pricing; not market prices",
        "stablecoins": {coin: 1 for coin in stablecoins},
        "stablecoin_registry_sha256": hashlib.sha256(json_bytes(dict(stablecoins), compact=True)).hexdigest(),
        "bank_fee_percent": 0 if result.provider in ("mastercard", "visa") else None,
        "additional_markup_percent": 0,
    })
    return files
