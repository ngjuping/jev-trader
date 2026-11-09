from __future__ import annotations

import time

from jev_trader.execution.bitfinex.book import BitfinexBook
from jev_trader.execution.bitfinex.messages import FLAG_OB_CHECKSUM, parse_public_trade
from jev_trader.state.events import BlockEvent, Trade


def control_action(message: object) -> str | None:
    """Map a WS info event to `reconnect`, `pause`, `resume`, or None."""
    if not isinstance(message, dict) or message.get("event") != "info":
        return None
    platform = message.get("platform")
    if isinstance(platform, dict) and platform.get("status") == 0:
        return "pause"
    code = message.get("code")
    if code == 20051:
        return "reconnect"
    if code == 20060:
        return "pause"
    if code == 20061:
        return "resume"
    return None


def decode_message(raw: object) -> object:
    if isinstance(raw, (bytes, bytearray)):
        import json

        return json.loads(raw.decode("utf-8"))
    if isinstance(raw, str):
        import json

        return json.loads(raw)
    return raw


class MarketSession:
    """Public book + trades session. Emits nothing the loop can quote until synced."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.book = BitfinexBook()
        self.synced = False
        self.needs_resubscribe = False
        self.api_errors = 0
        self.last_rx = 0.0
        self._channels: dict[int, str] = {}
        self._trades: list[Trade] = []
        self._updates = 0
        self._blind_since: float | None = None

    def reset(self) -> None:
        self.book.reset()
        self.synced = False
        self.needs_resubscribe = False
        self._channels.clear()
        self._trades.clear()
        self._updates = 0
        self._blind_since = time.time()

    def mark_unsynced(self) -> None:
        self.book.reset()
        self.synced = False
        self._trades.clear()
        self._updates = 0
        if self._blind_since is None:
            self._blind_since = time.time()

    def note_api_error(self) -> None:
        self.api_errors += 1

    def blind_for(self, now: float | None = None) -> float:
        if self.synced or self._blind_since is None:
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self._blind_since)

    def handle(self, message: object, *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        if isinstance(message, dict):
            self._handle_event(message)
            return
        if not isinstance(message, list) or len(message) < 2:
            return
        chan = message[0]
        if not isinstance(chan, int):
            return
        kind = self._channels.get(chan)
        payload = message[1]
        if payload == "hb":
            self.last_rx = stamp
            return
        if payload == "cs" and len(message) >= 3:
            self._handle_checksum(message[2])
            return
        if kind == "book":
            self._handle_book(payload, stamp)
        elif kind == "trades":
            self._handle_trades(message, stamp)

    def block_event(self, block: int) -> BlockEvent | None:
        if not self.synced:
            return None
        book = self.book.to_order_book(self.last_rx)
        if not book.two_sided:
            return None
        trades = tuple(self._trades)
        self._trades.clear()
        updates = self._updates
        self._updates = 0
        return BlockEvent(
            block=block,
            ts=self.last_rx,
            book=book,
            trades=trades,
            book_updates=updates,
        )

    def _handle_event(self, message: dict) -> None:
        if message.get("event") != "subscribed":
            return
        chan = message.get("chanId")
        channel = message.get("channel")
        if isinstance(chan, int) and isinstance(channel, str):
            self._channels[chan] = channel

    def _handle_book(self, payload: object, stamp: float) -> None:
        if not isinstance(payload, list):
            return
        kind = self.book.apply(payload)
        if kind == "ignore":
            return
        self.last_rx = stamp
        if kind == "snapshot":
            self.synced = self.book.synced
            self._blind_since = None
        elif kind == "update":
            self._updates += 1
            self.synced = self.book.synced

    def _handle_checksum(self, checksum: object) -> None:
        try:
            value = int(checksum)
        except (TypeError, ValueError):
            self.needs_resubscribe = True
            self.mark_unsynced()
            return
        if not self.book.verify_checksum(value):
            self.needs_resubscribe = True
            self.mark_unsynced()

    def _handle_trades(self, message: list, stamp: float) -> None:
        # Snapshot is `[chan, [[trade, ...]]]`. Incremental prints are
        # `[chan, "te"|"tu", trade]`. Only `te` is a new print; `tu` repeats it.
        if len(message) >= 3 and message[1] == "te" and isinstance(message[2], list):
            trade = parse_public_trade(message[2])
            if trade is not None:
                self._trades.append(trade)
                self.last_rx = stamp
            return
        if message[1] == "tu":
            self.last_rx = stamp


def subscribe_messages(symbol: str) -> tuple[dict[str, object], ...]:
    return (
        {"event": "conf", "flags": FLAG_OB_CHECKSUM},
        {
            "event": "subscribe",
            "channel": "book",
            "symbol": symbol,
            "prec": "P0",
            "freq": "F0",
            "len": "25",
        },
        {"event": "subscribe", "channel": "trades", "symbol": symbol},
    )
