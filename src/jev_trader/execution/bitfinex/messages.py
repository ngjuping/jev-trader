from __future__ import annotations

from dataclasses import dataclass

from jev_trader.execution.bitfinex.precision import format_number, round_price
from jev_trader.execution.bitfinex.symbols import order_type_for, pair_key
from jev_trader.state.events import Trade

FLAG_CLOSE = 512
FLAG_REDUCE_ONLY = 1024
FLAG_POST_ONLY = 4096
FLAG_OB_CHECKSUM = 131072

# Fee currencies we treat as already denominated in the quote (no conversion).
_QUOTE_LIKE = {"USD", "UST", "USDT", "USTF0", "USDF0", "EUR", "GBP", "JPY"}


@dataclass(frozen=True, slots=True)
class PairSpec:
    symbol: str
    min_order_size: float
    max_order_size: float | None


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee rates as fractions of notional. Positive is a cost, negative is a rebate."""

    maker: float
    taker: float

    @classmethod
    def fallback(cls, market: str) -> FeeSchedule:
        """Conservative defaults used when the account summary cannot be read.

        Derivatives assume a 2 bp maker cost (not the rebate some tiers receive)
        and a 6.5 bp taker cost. Spot assumes the base 10/20 bp schedule.
        """
        if market == "perp":
            return cls(maker=0.0002, taker=0.0065)
        return cls(maker=0.001, taker=0.002)


@dataclass(frozen=True, slots=True)
class WalletBalance:
    wallet_type: str
    currency: str
    balance: float
    available: float


@dataclass(frozen=True, slots=True)
class PositionView:
    symbol: str
    status: str
    amount: float
    base_price: float
    pl: float
    opened_ts: float | None


@dataclass(frozen=True, slots=True)
class RemoteOrder:
    id: int
    cid: int | None
    symbol: str
    amount: float
    price: float
    status: str
    flags: int

    @property
    def side(self) -> str:
        return "buy" if self.amount > 0 else "sell"

    @property
    def remaining(self) -> float:
        return abs(self.amount)


def parse_pair_spec(payload: object, symbol: str) -> PairSpec | None:
    """Parse `pub:info:pair` or `pub:info:pair:futures`.

    Shape: `[[ [PAIR, [FIRST_TRADE, PH, PH, MIN, MAX, ...]], ... ]]`.
    `MIN_ORDER_SIZE` is index 3 and `MAX_ORDER_SIZE` is index 4.
    """
    wanted = pair_key(symbol)
    for entry in _walk_pairs(payload):
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        name = entry[0]
        info = entry[1]
        if not isinstance(name, str) or not isinstance(info, list):
            continue
        if name != wanted and name != symbol:
            continue
        if len(info) < 5 or info[3] is None:
            return None
        min_size = float(info[3])
        max_size = float(info[4]) if info[4] not in (None, "") else None
        return PairSpec(symbol=symbol, min_order_size=min_size, max_order_size=max_size)
    return None


def _walk_pairs(payload: object) -> list:
    if not isinstance(payload, list):
        return []
    # Conf responses wrap the list once per requested key.
    if payload and isinstance(payload[0], list) and payload[0] and isinstance(payload[0][0], list):
        return list(payload[0])
    return list(payload)


def parse_fee_schedule(summary: object, *, market: str) -> FeeSchedule | None:
    """Parse `auth/r/summary` index 4.

    Maker info index 5 is the derivative maker rebate. Taker info index 5 is
    the derivative taker fee. Spot uses maker[0] and taker[1] (stable pairs).
    """
    if not isinstance(summary, list) or len(summary) < 5:
        return None
    fee_info = summary[4]
    if not isinstance(fee_info, list) or len(fee_info) < 2:
        return None
    maker_info, taker_info = fee_info[0], fee_info[1]
    if not isinstance(maker_info, list) or not isinstance(taker_info, list):
        return None
    try:
        if market == "perp":
            if len(maker_info) <= 5 or len(taker_info) <= 5:
                return None
            if maker_info[5] is None or taker_info[5] is None:
                return None
            rebate = float(maker_info[5])
            taker = float(taker_info[5])
            return FeeSchedule(maker=-abs(rebate), taker=abs(taker))
        if not maker_info or len(taker_info) <= 1:
            return None
        if maker_info[0] is None or taker_info[1] is None:
            return None
        return FeeSchedule(maker=float(maker_info[0]), taker=float(taker_info[1]))
    except (TypeError, ValueError):
        return None


def parse_wallets(payload: object) -> tuple[WalletBalance, ...]:
    if not isinstance(payload, list):
        return ()
    wallets: list[WalletBalance] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 3:
            continue
        available = float(row[4]) if len(row) > 4 and row[4] is not None else float(row[2])
        wallets.append(
            WalletBalance(
                wallet_type=str(row[0]),
                currency=str(row[1]),
                balance=float(row[2]),
                available=available,
            )
        )
    return tuple(wallets)


def parse_positions(payload: object) -> tuple[PositionView, ...]:
    if not isinstance(payload, list):
        return ()
    positions: list[PositionView] = []
    for row in payload:
        view = _parse_position_row(row)
        if view is not None:
            positions.append(view)
    return tuple(positions)


def _parse_position_row(row: object) -> PositionView | None:
    if not isinstance(row, list) or len(row) < 4:
        return None
    opened = None
    if len(row) > 12 and isinstance(row[12], (int, float)):
        opened = float(row[12]) / 1000.0
    pl = float(row[6]) if len(row) > 6 and row[6] is not None else 0.0
    return PositionView(
        symbol=str(row[0]),
        status=str(row[1]) if row[1] is not None else "",
        amount=float(row[2]),
        base_price=float(row[3]) if row[3] is not None else 0.0,
        pl=pl,
        opened_ts=opened,
    )


def parse_order_row(row: object) -> RemoteOrder | None:
    if not isinstance(row, list) or len(row) < 17:
        return None
    if row[0] is None or row[3] is None or row[6] is None:
        return None
    flags = int(row[12]) if row[12] is not None else 0
    cid = int(row[2]) if isinstance(row[2], (int, float)) else None
    price = float(row[16]) if row[16] is not None else 0.0
    return RemoteOrder(
        id=int(row[0]),
        cid=cid,
        symbol=str(row[3]),
        amount=float(row[6]),
        price=price,
        status=str(row[13]) if row[13] is not None else "",
        flags=flags,
    )


def is_live_status(status: str) -> bool:
    upper = status.upper()
    return upper.startswith("ACTIVE") or upper.startswith("PARTIALLY")


def parse_public_trade(row: list) -> Trade | None:
    """Public trade row: `[ID, MTS, AMOUNT, PRICE]`. Positive amount is a buy."""
    if len(row) < 4:
        return None
    try:
        amount = float(row[2])
        price = float(row[3])
        ts = float(row[1]) / 1000.0
    except (TypeError, ValueError):
        return None
    if amount == 0 or price <= 0:
        return None
    side = "buy" if amount > 0 else "sell"
    return Trade(ts=ts, price=price, size=abs(amount), side=side)


def fee_in_quote(fee: float, fee_currency: str | None, exec_price: float) -> float:
    """Convert an exchange fee to quote currency. Bitfinex fees are negative when charged."""
    cost = -fee
    if fee_currency and fee_currency.upper() not in _QUOTE_LIKE and exec_price > 0:
        return cost * exec_price
    return cost


def quote_body(
    *,
    cid: int,
    symbol: str,
    market: str,
    side: str,
    price: float,
    size: float,
    leverage: int | None,
) -> dict[str, object]:
    snapped = round_price(price, side=side)
    signed = size if side == "buy" else -size
    body: dict[str, object] = {
        "cid": cid,
        "type": order_type_for(market, marketable=False),
        "symbol": symbol,
        "amount": format_number(signed),
        "price": format_number(snapped, None if snapped >= 1 else 8),
        "flags": FLAG_POST_ONLY,
        "meta": {"protect_selfmatch": 1},
    }
    if market == "perp" and leverage is not None:
        body["lev"] = leverage
    return body


def replace_body(
    *,
    order_id: int,
    side: str,
    price: float,
    size: float,
    market: str,
    leverage: int | None,
) -> dict[str, object]:
    snapped = round_price(price, side=side)
    signed = size if side == "buy" else -size
    body: dict[str, object] = {
        "id": order_id,
        "price": format_number(snapped, None if snapped >= 1 else 8),
        "amount": format_number(signed),
        "flags": FLAG_POST_ONLY,
        "meta": {"protect_selfmatch": 1},
    }
    if market == "perp" and leverage is not None:
        body["lev"] = leverage
    return body


def flatten_body(
    *,
    cid: int,
    symbol: str,
    market: str,
    position: float,
    size: float,
    leverage: int | None,
) -> dict[str, object]:
    """Reduce-only market for perps. Spot uses an exchange market that only reduces."""
    closing_buy = position < 0
    signed = size if closing_buy else -size
    body: dict[str, object] = {
        "cid": cid,
        "type": order_type_for(market, marketable=True),
        "symbol": symbol,
        "amount": format_number(signed),
    }
    if market == "perp":
        body["flags"] = FLAG_REDUCE_ONLY
        if leverage is not None:
            body["lev"] = leverage
    return body
