from __future__ import annotations

import zlib

from jev_trader.execution.bitfinex.precision import checksum_number
from jev_trader.state.book import Level, OrderBook

_CHECKSUM_LEVELS = 25


def signed_crc32(payload: str) -> int:
    """CRC-32 as a signed int, matching Bitfinex's `crc-32` checksum."""
    value = zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


class BitfinexBook:
    """P0 aggregated book. Bids are positive amounts, asks are negative."""

    def __init__(self) -> None:
        self._bids: dict[float, tuple[int, float]] = {}
        self._asks: dict[float, tuple[int, float]] = {}
        self.synced = False

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.synced = False

    def apply(self, data: list) -> str:
        """Apply a snapshot or one update. Returns `snapshot`, `update`, or `ignore`."""
        if not data:
            return "ignore"
        if isinstance(data[0], list):
            self._bids.clear()
            self._asks.clear()
            for row in data:
                self._apply_row(row)
            self.synced = True
            return "snapshot"
        if len(data) >= 3 and not isinstance(data[0], str):
            self._apply_row(data)
            return "update"
        return "ignore"

    def _apply_row(self, row: list) -> None:
        if len(row) < 3:
            return
        price = float(row[0])
        count = int(row[1])
        amount = float(row[2])
        book = self._bids if amount > 0 else self._asks
        if count <= 0 or amount == 0:
            book.pop(price, None)
            return
        book[price] = (count, amount)

    def verify_checksum(self, checksum: int) -> bool:
        matches = signed_crc32(self.checksum_payload()) == int(checksum)
        if not matches:
            self.synced = False
        return matches

    def checksum_payload(self) -> str:
        bids = self._sorted_bids()[:_CHECKSUM_LEVELS]
        asks = self._sorted_asks()[:_CHECKSUM_LEVELS]
        parts: list[str] = []
        for index in range(max(len(bids), len(asks))):
            if index < len(bids):
                price, (_count, amount) = bids[index]
                parts.append(checksum_number(price))
                parts.append(checksum_number(amount))
            if index < len(asks):
                price, (_count, amount) = asks[index]
                parts.append(checksum_number(price))
                parts.append(checksum_number(amount))
        return ":".join(parts)

    def to_order_book(self, ts: float) -> OrderBook:
        bids = tuple(
            Level(price, abs(amount))
            for price, (_count, amount) in self._sorted_bids()
            if abs(amount) > 0
        )
        asks = tuple(
            Level(price, abs(amount))
            for price, (_count, amount) in self._sorted_asks()
            if abs(amount) > 0
        )
        return OrderBook(ts=ts, bids=bids, asks=asks)

    def _sorted_bids(self) -> list[tuple[float, tuple[int, float]]]:
        return sorted(self._bids.items(), key=lambda item: item[0], reverse=True)

    def _sorted_asks(self) -> list[tuple[float, tuple[int, float]]]:
        return sorted(self._asks.items(), key=lambda item: item[0])
