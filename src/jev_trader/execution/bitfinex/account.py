from __future__ import annotations

from dataclasses import dataclass, field

from jev_trader.execution.bitfinex.messages import (
    PositionView,
    RemoteOrder,
    fee_in_quote,
    is_live_status,
    parse_order_row,
    parse_positions,
)
from jev_trader.types import Fill, PnL


@dataclass
class TrackedOrder:
    id: int
    cid: int | None
    symbol: str
    side: str
    price: float
    remaining: float
    status: str
    flags: int


@dataclass
class Ack:
    ok: bool
    order_id: int | None = None
    cid: int | None = None
    error: str = ""


@dataclass
class AccountBook:
    """Order, fill, and position state driven by authenticated Bitfinex messages.

    Fills come from `tu` (the update that carries the fee). Order snapshots on
    connect replace the working set. Later `pu`/`ps` messages do not move the
    position; a periodic REST reconcile snaps it, so a `pu` that races a `tu`
    cannot double-count.
    """

    symbol: str
    orders: dict[int, TrackedOrder] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    seen_trades: set[int] = field(default_factory=set)
    position: float = 0.0
    avg_entry: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    position_opened_ts: float | None = None
    equity0: float = 0.0
    unrealized0: float = 0.0
    quote_balance: float = 0.0
    ready: bool = False
    api_errors: int = 0

    def unrealized(self, mid: float | None) -> float:
        if self.position == 0 or mid is None or mid <= 0:
            return 0.0
        return (mid - self.avg_entry) * self.position

    def pnl(self, mid: float | None) -> PnL:
        return PnL(
            realized=self.realized,
            unrealized=self.unrealized(mid),
            fees=self.fees,
        )

    def equity(self, mid: float | None) -> float:
        return self.equity0 + self.pnl(mid).net - self.unrealized0

    def set_opening_equity(self, quote_balance: float, unrealized: float) -> None:
        self.quote_balance = quote_balance
        self.equity0 = quote_balance + unrealized
        self.unrealized0 = unrealized

    def snap_position(self, amount: float, base_price: float, opened_ts: float | None) -> None:
        self.position = amount
        if amount == 0:
            self.avg_entry = 0.0
            self.position_opened_ts = None
            return
        self.avg_entry = base_price
        if opened_ts is not None:
            self.position_opened_ts = opened_ts
        elif self.position_opened_ts is None:
            self.position_opened_ts = 0.0

    def adopt_position(self, position: PositionView) -> None:
        if position.symbol != self.symbol:
            return
        self.snap_position(position.amount, position.base_price, position.opened_ts)

    def position_age_s(self, now: float) -> float:
        if self.position == 0 or self.position_opened_ts is None:
            return 0.0
        return max(0.0, now - self.position_opened_ts)

    def apply(self, message: object) -> list[Ack]:
        """Apply one WS or REST message. Returns acks for pending commands."""
        if isinstance(message, dict):
            return self._apply_event(message)
        if not isinstance(message, list) or len(message) < 2:
            return []
        # REST notifications are the inner array: [MTS, TYPE, ...].
        kind = message[1]
        rest_note = (
            isinstance(message[0], (int, float))
            and isinstance(kind, str)
            and kind.endswith("-req")
        )
        if rest_note:
            return self._apply_notification(message)
        if len(message) < 2 or message[0] != 0 or not isinstance(message[1], str):
            return []
        kind = message[1]
        if kind == "n" and len(message) >= 3:
            return self._apply_notification(message[2])
        if kind == "os" and len(message) >= 3 and isinstance(message[2], list):
            self._replace_orders(message[2])
            self.ready = True
            return []
        if kind in {"on", "ou", "oc"} and len(message) >= 3:
            return self._apply_order(message[2])
        if kind == "tu" and len(message) >= 3:
            self._apply_trade(message[2])
            return []
        if kind == "ps" and len(message) >= 3 and isinstance(message[2], list):
            for position in parse_positions(message[2]):
                if not self.ready and position.symbol == self.symbol:
                    self.adopt_position(position)
            return []
        return []

    def _apply_event(self, message: dict) -> list[Ack]:
        if message.get("event") == "auth" and message.get("status") == "OK":
            self.ready = True
        if message.get("event") == "error":
            self.api_errors += 1
        return []

    def _replace_orders(self, rows: list) -> None:
        self.orders.clear()
        for row in rows:
            remote = parse_order_row(row)
            if remote is None or remote.symbol != self.symbol:
                continue
            if not is_live_status(remote.status):
                continue
            self.orders[remote.id] = _tracked(remote)

    def _apply_order(self, row: object) -> list[Ack]:
        remote = parse_order_row(row)
        if remote is None or remote.symbol != self.symbol:
            return []
        if is_live_status(remote.status):
            self.orders[remote.id] = _tracked(remote)
            return [Ack(ok=True, order_id=remote.id, cid=remote.cid)]
        self.orders.pop(remote.id, None)
        error = remote.status if not is_live_status(remote.status) else ""
        failed = _is_reject(remote.status)
        return [Ack(ok=not failed, order_id=remote.id, cid=remote.cid, error=error)]

    def _apply_notification(self, note: object) -> list[Ack]:
        if not isinstance(note, list) or len(note) < 7:
            return []
        status = str(note[6]) if note[6] is not None else ""
        text = str(note[7]) if len(note) > 7 and note[7] is not None else status
        payload = note[4] if len(note) > 4 else None
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            acks: list[Ack] = []
            for row in payload:
                acks.extend(self._apply_order(row))
            if status.upper() == "SUCCESS":
                return acks or [Ack(ok=True)]
            return acks or [Ack(ok=False, error=text)]
        order = parse_order_row(payload) if payload is not None else None
        if status.upper() != "SUCCESS":
            if order is not None and order.symbol == self.symbol:
                self.orders.pop(order.id, None)
            cid = order.cid if order is not None else None
            oid = order.id if order is not None else None
            return [Ack(ok=False, order_id=oid, cid=cid, error=text or status)]
        if order is None:
            return [Ack(ok=True)]
        return self._apply_order(note[4])

    def _apply_trade(self, row: object) -> None:
        if not isinstance(row, list) or len(row) < 6:
            return
        try:
            trade_id = int(row[0])
            symbol = str(row[1])
            ts = float(row[2]) / 1000.0
            order_id = int(row[3]) if row[3] is not None else None
            amount = float(row[4])
            price = float(row[5])
        except (TypeError, ValueError):
            return
        if symbol != self.symbol or amount == 0 or trade_id in self.seen_trades:
            return
        self.seen_trades.add(trade_id)
        fee_raw = float(row[9]) if len(row) > 9 and row[9] is not None else 0.0
        fee_ccy = str(row[10]) if len(row) > 10 and row[10] is not None else None
        fee = fee_in_quote(fee_raw, fee_ccy, price)
        side = "buy" if amount > 0 else "sell"
        self._apply_fill(side, price, abs(amount), fee, ts)
        fill = Fill(
            side=side,
            price=price,
            size=abs(amount),
            fee=fee,
            ts=ts,
            order_id=str(order_id) if order_id is not None else None,
            trade_id=str(trade_id),
        )
        self.fills.append(fill)

    def drain_fills(self) -> tuple[Fill, ...]:
        drained = tuple(self.fills)
        self.fills.clear()
        return drained

    def _apply_fill(self, side: str, price: float, qty: float, fee: float, ts: float) -> None:
        self.fees += fee
        signed = qty if side == "buy" else -qty
        increasing = self.position == 0 or (self.position > 0) == (signed > 0)
        if increasing:
            new_position = self.position + signed
            if new_position != 0:
                self.avg_entry = (
                    abs(self.position) * self.avg_entry + qty * price
                ) / abs(new_position)
            self.position = new_position
            if self.position_opened_ts is None:
                self.position_opened_ts = ts
            return
        closing = min(abs(signed), abs(self.position))
        if self.position > 0:
            self.realized += (price - self.avg_entry) * closing
        else:
            self.realized += (self.avg_entry - price) * closing
        self.position += signed
        if abs(self.position) <= 1e-12:
            self.position = 0.0
            self.avg_entry = 0.0
            self.position_opened_ts = None
        elif abs(signed) > closing:
            self.avg_entry = price
            self.position_opened_ts = ts


def _tracked(remote: RemoteOrder) -> TrackedOrder:
    return TrackedOrder(
        id=remote.id,
        cid=remote.cid,
        symbol=remote.symbol,
        side=remote.side,
        price=remote.price,
        remaining=remote.remaining,
        status=remote.status,
        flags=remote.flags,
    )


def _is_reject(status: str) -> bool:
    upper = status.upper()
    return "POSTONLY" in upper or "INSUFFICIENT" in upper or upper.startswith("RSN_")
