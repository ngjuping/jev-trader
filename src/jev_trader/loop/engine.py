from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from jev_trader.calibration.fills import FillsLedger
from jev_trader.calibration.log import CalibrationLogger
from jev_trader.config import Settings
from jev_trader.execution.base import OrderRejected
from jev_trader.feeds.base import Feed
from jev_trader.judgment.fallback import HeuristicJudge
from jev_trader.policy.engine import compose_action
from jev_trader.pricing.avellaneda_stoikov import compute_quote
from jev_trader.risk.limits import RiskEngine, RiskState, Verdict, allowed_quote_sides, clamp_order_size_for_cap
from jev_trader.state.features import FeatureEngine
from jev_trader.state.micro import NEUTRAL, MicroSignal, MicroSignalEngine
from jev_trader.state.snapshot import MarketState, build_market_state
from jev_trader.types import Action, ActionKind, JudgmentSet, Order

logger = logging.getLogger(__name__)

QUOTING_ACTIONS = (ActionKind.QUOTE_BOTH_SIDES, ActionKind.QUOTE_WIDE, ActionKind.WIDEN)


@dataclass
class LoopStats:
    blocks: int = 0
    decisions: int = 0
    jev_decisions: int = 0
    fallback_decisions: int = 0
    timeouts: int = 0
    jev_errors: int = 0
    holds: int = 0
    kills: int = 0
    rejects: int = 0
    fills: int = 0
    fees: float = 0.0
    realized: float = 0.0
    unrealized: float = 0.0
    equity: float = 0.0
    actions: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    micro_one_sided_pulls: int = 0
    micro_toxic_pauses: int = 0
    micro_toxic_events: int = 0

    def summary(self) -> dict[str, Any]:
        latencies = sorted(self.latencies_ms)
        if latencies:
            average = sum(latencies) / len(latencies)
            p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
            maximum = latencies[-1]
        else:
            average = p95 = maximum = 0.0
        return {
            "blocks": self.blocks,
            "decisions": self.decisions,
            "jev_decisions": self.jev_decisions,
            "fallback_decisions": self.fallback_decisions,
            "timeouts": self.timeouts,
            "jev_errors": self.jev_errors,
            "holds": self.holds,
            "kills": self.kills,
            "rejects": self.rejects,
            "fills": self.fills,
            "actions": dict(sorted(self.actions.items())),
            "avg_latency_ms": round(average, 3),
            "p95_latency_ms": round(p95, 3),
            "max_latency_ms": round(maximum, 3),
            "realized": round(self.realized, 4),
            "unrealized": round(self.unrealized, 4),
            "fees": round(self.fees, 4),
            "equity": round(self.equity, 4),
            "micro_one_sided_pulls": self.micro_one_sided_pulls,
            "micro_toxic_pauses": self.micro_toxic_pauses,
            "micro_toxic_events": self.micro_toxic_events,
        }


class TradingLoop:
    """The nine-stage 24/7 loop with the block-deadline rule and fallback ladder."""

    def __init__(
        self,
        *,
        feed: Feed,
        venue: Any,
        judge: Any,
        fallback: HeuristicJudge,
        settings: Settings,
        calibration: CalibrationLogger | None = None,
        fills: FillsLedger | None = None,
    ) -> None:
        self._feed = feed
        self._venue = venue
        self._judge = judge
        self._fallback = fallback
        self._settings = settings
        self._calibration = calibration
        self._fills = fills
        self._risk = RiskEngine(settings.risk)
        self._features = FeatureEngine()
        pricing = settings.pricing
        self._micro = MicroSignalEngine(
            ofi_window_s=pricing.ofi_window_s,
            obi_levels=pricing.obi_levels,
            w_ofi=pricing.signal_w_ofi,
            w_obi=pricing.signal_w_obi,
            trade_window_s=pricing.toxic_trade_window_s,
            trade_min_samples=pricing.toxic_trade_min_samples,
            big_trade_quantile=pricing.toxic_trade_quantile,
            big_trade_median_mult=pricing.toxic_trade_median_mult,
            jump_bps=pricing.toxic_jump_bps,
            pause_s=pricing.toxic_pause_s,
        )
        self._micro_signal: MicroSignal = NEUTRAL
        self._last_book: Any = None
        self._last_micro_quote: dict[str, Any] = {}
        self._stats = LoopStats()
        self._peak_equity = settings.starting_cash
        self._api_errors = 0
        self._last_latency_ms = 0.0
        self._pending: deque[tuple[str, int, float]] = deque()
        self._killed = False

    @property
    def killed(self) -> bool:
        return self._killed

    async def run(self) -> LoopStats:
        try:
            async for event in self._feed.events():
                if self._killed:
                    break
                await self._tick(event)
        finally:
            await self._venue.cancel_all()
            pnl = self._venue.pnl()
            self._stats.realized = pnl.realized
            self._stats.unrealized = pnl.unrealized
            self._stats.fees = pnl.fees
            self._stats.equity = self._venue.equity()
        return self._stats

    async def _tick(self, event: Any) -> None:
        stats = self._stats
        stats.blocks += 1

        self._venue.on_block(event)
        new_fills = self._venue.drain_fills()
        stats.fills += len(new_fills)
        if self._fills is not None and event.book.two_sided:
            for fill in new_fills:
                self._fills.record_fill(ts=fill.ts or event.ts, mid=event.book.mid, fill=fill)

        now = time.time()
        data_age_s = max(0.0, now - event.ts)
        pnl = self._venue.pnl()
        equity = self._venue.equity()
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = (
            (self._peak_equity - equity) / self._peak_equity if self._peak_equity > 0 else 0.0
        )

        features = self._features.update(event)
        self._micro_signal = self._micro.update(event)
        self._last_book = event.book
        if self._micro_signal.toxic:
            stats.micro_toxic_events += 1
        health = self._venue.health()
        state = build_market_state(
            event,
            features,
            inventory=self._venue.inventory(),
            unrealized_pnl=pnl.unrealized,
            drawdown=drawdown,
            position_age_s=self._venue.position_age_s(event.ts),
            fill_ratio=health.fill_ratio,
            reject_rate=health.reject_rate,
            slippage_bps=health.slippage_bps,
            last_decision_latency_ms=self._last_latency_ms,
        )

        action, judgments, source, latency_ms = await self._decide(state)
        self._last_latency_ms = latency_ms
        stats.decisions += 1
        stats.latencies_ms.append(latency_ms)
        if source == "jev":
            stats.jev_decisions += 1
        elif source == "fallback":
            stats.fallback_decisions += 1

        if self._fills is not None:
            self._fills.observe(time.time(), state.mid)
        venue_errors = int(getattr(self._venue, "api_errors", 0))
        verdict = self._risk.check(
            RiskState(
                position=self._venue.inventory(),
                daily_pnl=pnl.net,
                drawdown=drawdown,
                inventory_age_s=self._venue.position_age_s(event.ts),
                data_age_s=data_age_s,
                decision_latency_ms=latency_ms,
                api_errors=self._api_errors + venue_errors,
                mid=state.mid,
            )
        )

        if verdict.verdict is Verdict.KILL:
            stats.kills += 1
            self._killed = True
            logger.critical("kill switch: %s", "; ".join(verdict.breaches))
            await self._venue.flatten()
            return

        reduce_only = verdict.verdict is Verdict.REDUCE_ONLY
        if verdict.verdict is Verdict.HOLD:
            stats.holds += 1
            action = Action(ActionKind.HOLD, reason="; ".join(verdict.breaches))
        elif reduce_only:
            action = Action(
                action.kind,
                size_scale=action.size_scale * self._settings.policy.low_confidence_scale,
                skew=action.skew,
                reason=f"{action.reason} | reduce_only: {'; '.join(verdict.breaches)}",
            )

        await self._execute(action, state, reduce_only)

        stats.actions[action.kind.value] = stats.actions.get(action.kind.value, 0) + 1
        self._log_micro(event.block, state)

        if self._calibration is not None:
            decision_id = self._calibration.record_decision(
                block=event.block,
                ts=event.ts,
                mid=state.mid,
                state=state.to_state_dict(),
                judgments=judgments,
                action=action,
                latency_ms=latency_ms,
                source=source,
            )
            if judgments is not None:
                self._pending.append((decision_id, event.block, state.mid))
            self._label_matured(event.block, state.mid)

    async def _decide(self, state: MarketState) -> tuple[Action, JudgmentSet | None, str, float]:
        started = time.perf_counter()
        try:
            judgments = await asyncio.wait_for(
                self._judge.judge(state),
                timeout=self._settings.decision_timeout_s,
            )
        except asyncio.TimeoutError:
            latency_ms = (time.perf_counter() - started) * 1000.0
            self._stats.timeouts += 1
            action = Action(ActionKind.HOLD, reason="decision past block deadline")
            return action, None, "timeout", latency_ms
        except Exception:
            latency_ms = (time.perf_counter() - started) * 1000.0
            self._api_errors += 1
            self._stats.jev_errors += 1
            logger.exception("Jev unavailable, falling back to deterministic judgments")
            judgments = await self._fallback.judge(state)
            action = compose_action(judgments, state, self._settings.policy, self._settings.risk)
            return action, judgments, "fallback", latency_ms

        latency_ms = (time.perf_counter() - started) * 1000.0
        action = compose_action(judgments, state, self._settings.policy, self._settings.risk)
        return action, judgments, judgments.source, latency_ms

    async def _execute(self, action: Action, state: MarketState, reduce_only: bool) -> None:
        # After an ack/cancel timeout the venue is orders-blind: pull only, no new quotes.
        if getattr(self._venue, "orders_blind", False):
            orders: list[Order] = []
        else:
            orders = self._quote_orders(action, state, reduce_only)
        sync = getattr(self._venue, "sync_quotes", None)
        if sync is not None:
            await self._guarded(sync(tuple(orders)))
            return
        if not await self._guarded(self._venue.cancel_all()):
            return
        for order in orders:
            try:
                await self._venue.place(order)
            except OrderRejected:
                self._stats.rejects += 1
            except Exception as exc:
                self._count_api_error(exc)

    def _quote_orders(
        self,
        action: Action,
        state: MarketState,
        reduce_only: bool,
    ) -> list[Order]:
        if action.kind not in QUOTING_ACTIONS:
            return []
        if action.kind in (ActionKind.QUOTE_WIDE, ActionKind.WIDEN):
            multiplier = self._settings.policy.wide_spread_multiplier
        else:
            multiplier = 1.0
        pricing = self._settings.pricing
        micro = self._micro_signal
        if micro.paused(state.ts):
            self._stats.micro_toxic_pauses += 1
            self._last_micro_quote = {"pulled": "both", "reason": micro.toxic_reason or "toxic pause"}
            return []
        max_position = max(self._settings.risk.max_position, 1e-9)
        center = state.mid
        if pricing.use_microprice and state.microprice > 0:
            center = state.microprice
        center += center * pricing.micro_alpha_bps * micro.signal / 10_000.0
        quote = compute_quote(
            mid=center,
            inventory=state.inventory / max_position,
            sigma=max(state.realized_vol_short * state.mid, 1e-9),  # price units ($/sqrt(s))
            tau=pricing.horizon_s,
            gamma=pricing.gamma,
            kappa=pricing.kappa,
            min_half_spread=state.mid * pricing.min_half_spread_bps / 10_000.0,
            skew=action.skew,
            half_spread_multiplier=multiplier,
            tick_size=pricing.tick_size,
        )
        bid_price, ask_price = quote.bid, quote.ask
        book = self._last_book
        if pricing.clamp_to_touch and book is not None and getattr(book, "two_sided", False):
            bid_price = min(bid_price, book.bids[0].price)
            ask_price = max(ask_price, book.asks[0].price)
        size = self._settings.risk.max_order_size * action.size_scale
        notional_cap = self._settings.risk.max_notional
        if notional_cap > 0 and state.mid > 0:
            size = min(size, notional_cap / state.mid)
        if size <= 0:
            return []
        want_bid = not reduce_only or state.inventory < 0
        want_ask = not reduce_only or state.inventory > 0
        # Hard inventory gate: projected inv (incl. open quotes) must stay within max_position.
        open_buy = 0.0
        open_sell = 0.0
        open_fn = getattr(self._venue, "open_orders", None)
        if callable(open_fn):
            for resting in open_fn():
                if resting.side == "buy":
                    open_buy += resting.size
                else:
                    open_sell += resting.size
        gate_bid, gate_ask = allowed_quote_sides(
            state.inventory,
            max_position,
            open_buy=open_buy,
            open_sell=open_sell,
        )
        want_bid = want_bid and gate_bid
        want_ask = want_ask and gate_ask
        pulled = ""
        threshold = pricing.one_sided_threshold
        if threshold > 0 and abs(micro.signal) > threshold:
            # Strong sell pressure -> our bid gets picked off; strong buy -> our ask.
            if micro.signal < 0 and want_bid:
                want_bid = False
                pulled = "bid"
            elif micro.signal > 0 and want_ask:
                want_ask = False
                pulled = "ask"
            if pulled:
                self._stats.micro_one_sided_pulls += 1
        self._last_micro_quote = {
            "pulled": pulled,
            "center": round(center, 2),
            "bid": round(bid_price, 2) if want_bid else None,
            "ask": round(ask_price, 2) if want_ask else None,
        }
        orders: list[Order] = []
        if want_bid:
            bid_size = clamp_order_size_for_cap(
                "buy", size, state.inventory, max_position,
                open_buy=open_buy, open_sell=open_sell,
            )
            if bid_size > 0:
                orders.append(Order(side="buy", price=bid_price, size=bid_size))
        if want_ask:
            ask_size = clamp_order_size_for_cap(
                "sell", size, state.inventory, max_position,
                open_buy=open_buy, open_sell=open_sell,
            )
            if ask_size > 0:
                orders.append(Order(side="sell", price=ask_price, size=ask_size))
        return orders

    def _log_micro(self, block: int, state: MarketState) -> None:
        every = self._settings.pricing.micro_log_every_blocks
        micro = self._micro_signal
        if every <= 0 or (block % every and not micro.toxic):
            return
        stats = self._stats
        payload = {
            "block": block,
            "mid": round(state.mid, 2),
            "micro": round(micro.microprice, 2),
            "ofi": round(micro.ofi, 6),
            "ofi_norm": round(micro.ofi_norm, 4),
            "obi": round(micro.obi, 4),
            "s": round(micro.signal, 4),
            "jump_bps": round(micro.jump_bps, 3),
            "toxic": micro.toxic_reason,
            "paused": micro.paused(state.ts),
            "inv": state.inventory,
            "quote": self._last_micro_quote,
            "pulls": stats.micro_one_sided_pulls,
            "toxic_pauses": stats.micro_toxic_pauses,
            "toxic_events": stats.micro_toxic_events,
            "equity": round(self._venue.equity(), 4),
        }
        logger.info("micro %s", json.dumps(payload, separators=(",", ":")))

    async def _guarded(self, awaitable: Any) -> bool:
        try:
            await awaitable
        except OrderRejected:
            self._stats.rejects += 1
            return False
        except Exception as exc:
            self._count_api_error(exc)
            return False
        return True

    def _count_api_error(self, exc: Exception) -> None:
        if not getattr(exc, "counted", False):
            self._api_errors += 1
        logger.exception("venue call failed")

    def _label_matured(self, block: int, mid: float) -> None:
        if self._calibration is None:
            return
        horizon = self._settings.calibration_horizon_blocks
        while self._pending and block - self._pending[0][1] >= horizon:
            decision_id, _, mid_then = self._pending.popleft()
            ret = mid / mid_then - 1.0 if mid_then > 0 else 0.0
            if ret > 1e-4:
                outcome = "up"
            elif ret < -1e-4:
                outcome = "down"
            else:
                outcome = "neutral"
            self._calibration.record_outcome(
                decision_id=decision_id,
                horizon_blocks=horizon,
                mid_then=mid_then,
                mid_now=mid,
                outcome=outcome,
            )
