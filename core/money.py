"""Decimal-backed money helpers.

The public Pydantic models still expose floats for simple JSON/config
compatibility, but arithmetic should pass through these helpers so USDC,
share, and price calculations do not accumulate binary float noise.
"""

from decimal import Decimal, ROUND_HALF_UP


USDC_QUANT = Decimal("0.000001")
SHARE_QUANT = Decimal("0.000001")
PRICE_QUANT = Decimal("0.0001")
PCT_QUANT = Decimal("0.0001")


def dec(value) -> Decimal:
    """Convert numeric-ish values to Decimal without binary float artifacts."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def quantize(value, quantum: Decimal) -> Decimal:
    return dec(value).quantize(quantum, rounding=ROUND_HALF_UP)


def usdc(value) -> float:
    return float(quantize(value, USDC_QUANT))


def shares(value) -> float:
    return float(quantize(value, SHARE_QUANT))


def price(value) -> float:
    return float(quantize(value, PRICE_QUANT))


def pct(value) -> float:
    return float(quantize(value, PCT_QUANT))
