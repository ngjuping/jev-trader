"""Local microstructure signals for anti-adverse-selection quoting.

Everything here is deterministic and computed per block from the public book and
trades; nothing waits for the Jev judgment call.

* OFI: Cont-Kukanov-Stoikov order-flow imbalance from best bid/ask price and
  size changes between consecutive blocks, summed over a rolling window and
  normalized by the average top-of-book depth over the same window.
* OBI: depth imbalance of the first ``obi_levels`` levels (book.imbalance).
* signal s in [-1, 1]: weighted blend of tanh(normalized OFI) and OBI.
* toxicity: a large aggressive trade (size >= max(p_q, mult * median) of the
  rolling trade sizes) or a sharp mid jump between blocks starts a pause
  during which neither side is quoted.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from jev_trader.state.book import OrderBook
from jev_trader.state.events import BlockEvent


def ofi_increment(prev: OrderBook, cur: OrderBook) -> float:
    """CKS order-flow increment e_n between two two-sided books (in base units).

    e = 1{Pb>=Pb'} qb - 1{Pb<=Pb'} qb' - 1{Pa<=Pa'} qa + 1{Pa>=Pa'} qa'
    Positive = net buy pressure (bids added / asks consumed).
    """
    if not (prev.two_sided and cur.two_sided):
        return 0.0
    pb0, qb0 = prev.bids[0].price, prev.bids[0].size
    pa0, qa0 = prev.asks[0].price, prev.asks[0].size
    pb1, qb1 = cur.bids[0].price, cur.bids[0].size
    pa1, qa1 = cur.asks[0].price, cur.asks[0].size
    e = 0.0
    if pb1 >= pb0:
        e += qb1
    if pb1 <= pb0:
        e -= qb0
    if pa1 <= pa0:
        e -= qa1
    if pa1 >= pa0:
        e += qa0
    return e


def combine_signal(ofi_norm: float, obi: float, *, w_ofi: float, w_obi: float) -> float:
    total = abs(w_ofi) + abs(w_obi)
    if total <= 0:
        return 0.0
    raw = (w_ofi * math.tanh(ofi_norm) + w_obi * obi) / total
    return max(-1.0, min(1.0, raw))


@dataclass(frozen=True, slots=True)
class MicroSignal:
    ts: float
    microprice: float
    ofi: float
    ofi_norm: float
    obi: float
    signal: float
    jump_bps: float
    toxic: bool
    toxic_reason: str
    paused_until: float

    def paused(self, now: float) -> bool:
        return now < self.paused_until


NEUTRAL = MicroSignal(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, "", 0.0)


class MicroSignalEngine:
    def __init__(
        self,
        *,
        ofi_window_s: float = 3.0,
        obi_levels: int = 3,
        w_ofi: float = 0.5,
        w_obi: float = 0.5,
        trade_window_s: float = 300.0,
        trade_min_samples: int = 50,
        big_trade_quantile: float = 0.95,
        big_trade_median_mult: float = 3.0,
        jump_bps: float = 3.0,
        pause_s: float = 1.5,
    ) -> None:
        self.ofi_window_s = ofi_window_s
        self.obi_levels = obi_levels
        self.w_ofi = w_ofi
        self.w_obi = w_obi
        self.trade_window_s = trade_window_s
        self.trade_min_samples = trade_min_samples
        self.big_trade_quantile = big_trade_quantile
        self.big_trade_median_mult = big_trade_median_mult
        self.jump_bps = jump_bps
        self.pause_s = pause_s
        self._prev: OrderBook | None = None
        self._ofi: deque[tuple[float, float, float]] = deque()  # ts, e, top depth
        self._trades: deque[tuple[float, float]] = deque()  # ts, size
        self._paused_until = 0.0
        self.last: MicroSignal = NEUTRAL
        self.toxic_events = 0
        self.toxic_trade_events = 0
        self.toxic_jump_events = 0

    def _big_trade_threshold(self) -> float | None:
        if len(self._trades) < self.trade_min_samples:
            return None
        sizes = sorted(size for _, size in self._trades)
        n = len(sizes)
        q = sizes[min(n - 1, int(self.big_trade_quantile * n))]
        median = sizes[n // 2]
        return max(q, self.big_trade_median_mult * median)

    def update(self, event: BlockEvent) -> MicroSignal:
        book = event.book
        now = event.ts
        if not book.two_sided:
            self._prev = None
            return self.last
        micro = book.microprice
        top_depth = (book.bids[0].size + book.asks[0].size) / 2.0

        jump_bps = 0.0
        e = 0.0
        prev = self._prev
        if prev is not None and prev.two_sided:
            e = ofi_increment(prev, book)
            # Jump on mid, not microprice: microprice flips inside the spread with
            # top-size noise (~2 bp spread on BTCF0) and would pause far too often.
            prev_mid = prev.mid
            if prev_mid > 0:
                jump_bps = 10_000.0 * (book.mid / prev_mid - 1.0)
        self._prev = book
        self._ofi.append((now, e, top_depth))
        cutoff = now - self.ofi_window_s
        while self._ofi and self._ofi[0][0] < cutoff:
            self._ofi.popleft()
        ofi = sum(item[1] for item in self._ofi)
        depth = sum(item[2] for item in self._ofi) / len(self._ofi)
        ofi_norm = ofi / depth if depth > 0 else 0.0
        obi = book.imbalance(self.obi_levels)
        signal = combine_signal(ofi_norm, obi, w_ofi=self.w_ofi, w_obi=self.w_obi)

        # Toxicity: evaluate this block's trades against the history *before* them.
        reasons: list[str] = []
        threshold = self._big_trade_threshold()
        if threshold is not None and event.trades:
            biggest = max(trade.size for trade in event.trades)
            if biggest >= threshold:
                reasons.append(f"big_trade {biggest:.6g}>={threshold:.6g}")
                self.toxic_trade_events += 1
        for trade in event.trades:
            self._trades.append((trade.ts or now, abs(trade.size)))
        tcut = now - self.trade_window_s
        while self._trades and self._trades[0][0] < tcut:
            self._trades.popleft()
        if self.jump_bps > 0 and abs(jump_bps) >= self.jump_bps:
            reasons.append(f"jump {jump_bps:+.2f}bp")
            self.toxic_jump_events += 1
        toxic = bool(reasons)
        if toxic:
            self.toxic_events += 1
            self._paused_until = max(self._paused_until, now + self.pause_s)

        self.last = MicroSignal(
            ts=now,
            microprice=micro,
            ofi=ofi,
            ofi_norm=ofi_norm,
            obi=obi,
            signal=signal,
            jump_bps=jump_bps,
            toxic=toxic,
            toxic_reason="; ".join(reasons),
            paused_until=self._paused_until,
        )
        return self.last
