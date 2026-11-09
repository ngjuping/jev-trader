from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jev_trader.config import RiskConfig


class Verdict(str, Enum):
    OK = "ok"
    REDUCE_ONLY = "reduce_only"
    HOLD = "hold"
    KILL = "kill"


_PRECEDENCE = {
    Verdict.OK: 0,
    Verdict.REDUCE_ONLY: 1,
    Verdict.HOLD: 2,
    Verdict.KILL: 3,
}


@dataclass(frozen=True, slots=True)
class RiskState:
    position: float = 0.0
    daily_pnl: float = 0.0
    drawdown: float = 0.0
    inventory_age_s: float = 0.0
    data_age_s: float = 0.0
    decision_latency_ms: float = 0.0
    api_errors: int = 0
    mid: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    verdict: Verdict
    breaches: tuple[str, ...] = ()


class RiskEngine:
    """Hard-coded deterministic vetoes, checked before every order, zero negotiation.

    Every limit is checkable by something other than the model's own claim.
    """

    def __init__(self, limits: RiskConfig) -> None:
        self._limits = limits

    def check(self, state: RiskState) -> RiskVerdict:
        limits = self._limits
        breaches: list[str] = []
        verdict = Verdict.OK

        def escalate(candidate: Verdict, reason: str) -> None:
            nonlocal verdict
            breaches.append(reason)
            if _PRECEDENCE[candidate] > _PRECEDENCE[verdict]:
                verdict = candidate

        if state.api_errors > limits.max_api_errors:
            escalate(
                Verdict.KILL,
                f"api errors {state.api_errors} > {limits.max_api_errors}",
            )
        if state.drawdown > limits.max_drawdown:
            escalate(Verdict.KILL, f"drawdown {state.drawdown:.4f} > {limits.max_drawdown}")
        if state.daily_pnl < -limits.max_daily_loss:
            escalate(
                Verdict.KILL,
                f"daily loss {-state.daily_pnl:.2f} > {limits.max_daily_loss}",
            )

        if abs(state.position) > limits.max_position * limits.kill_position_multiplier:
            escalate(Verdict.KILL, f"position {state.position:.2f} beyond kill multiplier")
        elif abs(state.position) > limits.max_position:
            escalate(Verdict.REDUCE_ONLY, f"position {state.position:.2f} > max")

        if limits.max_notional > 0 and state.mid > 0:
            notional = abs(state.position) * state.mid
            kill_at = limits.max_notional * limits.kill_position_multiplier
            if notional > kill_at:
                escalate(
                    Verdict.KILL,
                    f"notional {notional:.2f} > {kill_at:.2f}",
                )
            elif notional > limits.max_notional:
                escalate(
                    Verdict.REDUCE_ONLY,
                    f"notional {notional:.2f} > {limits.max_notional:.2f}",
                )

        if state.inventory_age_s > limits.max_inventory_age_s:
            escalate(
                Verdict.REDUCE_ONLY,
                f"inventory age {state.inventory_age_s:.0f}s > {limits.max_inventory_age_s:.0f}s",
            )
        if state.data_age_s > limits.max_stale_data_age_s:
            escalate(
                Verdict.HOLD,
                f"stale data {state.data_age_s:.3f}s > {limits.max_stale_data_age_s}s",
            )
        if state.decision_latency_ms > limits.max_decision_latency_ms:
            escalate(
                Verdict.HOLD,
                f"decision latency {state.decision_latency_ms:.0f}ms > "
                f"{limits.max_decision_latency_ms:.0f}ms",
            )

        return RiskVerdict(verdict=verdict, breaches=tuple(breaches))
