from __future__ import annotations

import asyncio
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
from jev_trader.risk.limits import RiskEngine, RiskState, Verdict
from jev_trader.state.features import FeatureEngine
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
        max_position = max(self._settings.risk.max_position, 1e-9)
        quote = compute_quote(
            mid=state.mid,
            inventory=state.inventory / max_position,
            sigma=max(state.realized_vol_short, 1e-9),
            tau=self._settings.pricing.horizon_s,
            gamma=self._settings.pricing.gamma,
            kappa=self._settings.pricing.kappa,
            min_half_spread=state.mid * self._settings.pricing.min_half_spread_bps / 10_000.0,
            skew=action.skew,
            half_spread_multiplier=multiplier,
            tick_size=self._settings.pricing.tick_size,
        )
        size = self._settings.risk.max_order_size * action.size_scale
        notional_cap = self._settings.risk.max_notional
        if notional_cap > 0 and state.mid > 0:
            size = min(size, notional_cap / state.mid)
        if size <= 0:
            return []
        orders: list[Order] = []
        if not reduce_only or state.inventory < 0:
            orders.append(Order(side="buy", price=quote.bid, size=size))
        if not reduce_only or state.inventory > 0:
            orders.append(Order(side="sell", price=quote.ask, size=size))
        return orders

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
