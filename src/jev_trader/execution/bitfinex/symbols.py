from __future__ import annotations

_KNOWN_QUOTES = ("USDT", "UST", "USD", "EUR", "GBP", "BTC", "ETH", "JPY", "TRY")


def is_derivative_symbol(symbol: str) -> bool:
    """Perpetuals use the `F0` suffix, e.g. `tBTCF0:USTF0`."""
    return "F0" in symbol


def split_symbol(symbol: str) -> tuple[str, str]:
    """Return `(base, quote)` for a trading symbol. The leading `t` is stripped."""
    raw = symbol[1:] if symbol.startswith("t") else symbol
    if ":" in raw:
        base, quote = raw.split(":", 1)
        return base, quote
    for quote in _KNOWN_QUOTES:
        if raw.endswith(quote) and len(raw) > len(quote):
            return raw[: -len(quote)], quote
    return raw, "USD"


def pair_key(symbol: str) -> str:
    """Conf endpoints key pairs without the leading `t`."""
    return symbol[1:] if symbol.startswith("t") else symbol


def order_type_for(market: str, *, marketable: bool) -> str:
    """Spot posts on the exchange wallet. Derivatives and margin use LIMIT/MARKET."""
    if market == "spot":
        return "EXCHANGE MARKET" if marketable else "EXCHANGE LIMIT"
    if market == "perp":
        return "MARKET" if marketable else "LIMIT"
    raise ValueError(f"unknown market {market}")


def market_for_symbol(symbol: str, override: str | None = None) -> str:
    if override in ("spot", "perp"):
        return override
    return "perp" if is_derivative_symbol(symbol) else "spot"
