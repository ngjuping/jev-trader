from __future__ import annotations

import math

from jev_trader.execution.base import OrderRejected

# Bitfinex prices use 5 significant figures, capped at 8 decimal places.
# The published rule counts digits to the left of the decimal:
# https://docs.bitfinex.com/docs/abbreviations-glossary (significant digits)
_SIG_FIGS = 5
_MAX_DECIMALS = 8


def price_decimals(price: float) -> int:
    """Decimal places Bitfinex accepts for a price at this magnitude."""
    if price <= 0:
        return _MAX_DECIMALS
    digits = math.floor(math.log10(price)) + 1
    return max(0, min(_MAX_DECIMALS, _SIG_FIGS - digits))


def price_quantum(price: float) -> float:
    decimals = price_decimals(price)
    if decimals == 0:
        return 1.0
    return 10.0 ** (-decimals)


def round_price(price: float, *, side: str) -> float:
    """Snap to the venue increment. Buys round down and sells round up (stay passive)."""
    if price <= 0:
        raise OrderRejected(f"price {price} is not positive")
    quantum = price_quantum(price)
    if side == "buy":
        snapped = math.floor(price / quantum + 1e-9) * quantum
    elif side == "sell":
        snapped = math.ceil(price / quantum - 1e-9) * quantum
    else:
        raise OrderRejected(f"unknown side {side}")
    decimals = price_decimals(price)
    snapped = round(snapped, decimals)
    if snapped <= 0:
        raise OrderRejected(f"price {price} rounds to a non-positive {side} price")
    return snapped


def round_size(
    size: float,
    *,
    min_size: float,
    max_size: float | None = None,
    decimals: int = 8,
) -> float:
    """Round an order size down to the venue amount increment and enforce min/max."""
    if size <= 0:
        raise OrderRejected("size must be positive")
    quantum = 10.0 ** (-decimals)
    snapped = math.floor(size / quantum + 1e-9) * quantum
    snapped = round(snapped, decimals)
    if snapped < min_size:
        raise OrderRejected(f"size {snapped} is below minimum {min_size}")
    if max_size is not None and snapped > max_size:
        raise OrderRejected(f"size {snapped} is above maximum {max_size}")
    return snapped


def format_number(value: float, decimals: int | None = None) -> str:
    """Decimal string without scientific notation or trailing zeros."""
    places = _MAX_DECIMALS if decimals is None else decimals
    rendered = f"{value:.{places}f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def checksum_number(value: float) -> str:
    """Number format used in the book checksum string (no scientific notation)."""
    return format_number(value, _MAX_DECIMALS)
