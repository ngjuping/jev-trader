from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionKind(str, Enum):
    STAND_DOWN = "stand_down"
    QUOTE_WIDE = "quote_wide"
    QUOTE_BOTH_SIDES = "quote_both_sides"
    PULL_QUOTES = "pull_quotes"
    WIDEN = "widen"
    HOLD = "hold"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class JudgmentSet:
    regime: str = "unknown"
    regime_confidence: float = 0.0
    regime_probabilities: dict[str, float] = field(default_factory=dict)
    direction: str = "neutral"
    direction_confidence: float = 0.0
    direction_probabilities: dict[str, float] = field(default_factory=dict)
    toxic_flow: float = 0.5
    liquidity_stressed: float = 0.5
    quote_environment: float = 0.0
    quote_environment_confidence: float = 0.0
    inventory_pressure: float = 0.0
    inventory_pressure_confidence: float = 0.0
    model: str = "none"
    latency_ms: float = 0.0
    request_id: str | None = None
    source: str = "fallback"

    @property
    def p_up(self) -> float:
        return self.direction_probabilities.get("up", 0.5)


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    size_scale: float = 1.0
    skew: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Order:
    side: str
    price: float
    size: float
    post_only: bool = True


@dataclass(frozen=True, slots=True)
class Fill:
    side: str
    price: float
    size: float
    fee: float
    ts: float | None = None
    order_id: str | None = None
    trade_id: str | None = None


@dataclass(frozen=True, slots=True)
class PnL:
    realized: float = 0.0
    unrealized: float = 0.0
    fees: float = 0.0

    @property
    def net(self) -> float:
        return self.realized + self.unrealized - self.fees
