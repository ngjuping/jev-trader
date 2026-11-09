from __future__ import annotations

import asyncio
import signal
import time

import pytest
from tests.bitfinex_support import FakeREST, ScriptedSend

from jev_trader.calibration.log import CalibrationLogger
from jev_trader.config import RiskConfig, Settings
from jev_trader.execution.bitfinex.errors import BitfinexAPIError
from jev_trader.execution.bitfinex.messages import FeeSchedule
from jev_trader.execution.bitfinex.venue import BitfinexVenue
from jev_trader.execution.paper import PaperVenue
from jev_trader.judgment.fallback import HeuristicJudge
from jev_trader.loop.bitfinex_run import install_signal_handlers
from jev_trader.loop.engine import TradingLoop
from jev_trader.risk.limits import RiskEngine, RiskState, Verdict
from jev_trader.state.book import Level, OrderBook
from jev_trader.state.events import BlockEvent
from jev_trader.types import Order

SYMBOL = "tBTCF0:USTF0"


def _fresh(send: ScriptedSend) -> BitfinexVenue:
    venue = BitfinexVenue(
        symbol=SYMBOL,
        market="perp",
        send=send,
        rest=FakeREST(),
        fees=FeeSchedule(0.0002, 0.0065),
        min_order_size=0.0004,
        max_order_size=1.0,
        max_stale_s=5.0,
        leverage=1,
        ack_timeout_s=0.05,
    )
    send.venue = venue
    venue.on_block(
        BlockEvent(
            block=0,
            ts=time.time(),
            book=OrderBook(ts=time.time(), bids=(Level(99.0, 1.0),), asks=(Level(101.0, 1.0),)),
        )
    )
    return venue


async def test_flatten_cancels_then_sends_reduce_only():
    send = ScriptedSend()
    venue = _fresh(send)
    await venue.place(Order(side="buy", price=99.0, size=0.01))
    venue.adopt_remote(orders=(), position_amount=0.01, base_price=99.0, quote_balance=200.0)
    # adopt_remote snaps the position but the resting quote is still open.
    await venue.place(Order(side="buy", price=98.0, size=0.01))
    send.sent.clear()
    fill = await venue.flatten()
    assert venue.halted
    assert send.sent[0][1] == "oc_multi"
    flatten = send.sent[-1][3]
    assert flatten["type"] == "MARKET"
    assert flatten["flags"] == 1024
    assert float(flatten["amount"]) < 0
    assert fill is not None
    assert fill.side == "sell"
    assert venue.inventory() == pytest.approx(0.0)
    assert await venue.cancel_all() == 0


async def test_ack_timeout_counts_as_an_api_error():
    send = ScriptedSend()
    venue = _fresh(send)
    send.venue = None
    with pytest.raises(BitfinexAPIError):
        await venue.place(Order(side="buy", price=99.0, size=0.01))
    assert venue.api_errors >= 1


def test_notional_cap_reduces_then_kills():
    limits = RiskConfig(max_notional=50.0, max_position=100.0)
    engine = RiskEngine(limits)
    assert engine.check(RiskState(position=0.0004, mid=100_000)).verdict is Verdict.OK
    over = engine.check(RiskState(position=0.0006, mid=100_000))
    assert over.verdict is Verdict.REDUCE_ONLY
    killed = engine.check(RiskState(position=0.001, mid=100_000))
    assert killed.verdict is Verdict.KILL


async def test_repeated_venue_api_errors_trip_the_loop_kill(make_event, tmp_path):
    class Feed:
        def __init__(self) -> None:
            self._done = False

        async def events(self):
            if not self._done:
                self._done = True
                yield make_event(ts=time.time())

    venue = PaperVenue(starting_cash=1_000.0, fee_bps=0.0, tick_size=0.01, max_order_size=10.0)
    venue.api_errors = 10
    settings = Settings(
        log_path=tmp_path / "cal.jsonl",
        risk=RiskConfig(max_api_errors=5),
    )
    loop = TradingLoop(
        feed=Feed(),
        venue=venue,
        judge=HeuristicJudge(max_position=settings.risk.max_position),
        fallback=HeuristicJudge(max_position=settings.risk.max_position),
        settings=settings,
        calibration=CalibrationLogger(tmp_path / "cal.jsonl"),
    )
    stats = await loop.run()
    assert stats.kills == 1
    assert loop.killed


def test_sigterm_handler_sets_the_stop_event():
    handlers: dict[signal.Signals, object] = {}

    class _Loop:
        def add_signal_handler(self, sig: signal.Signals, callback: object) -> None:
            handlers[sig] = callback

    stop = asyncio.Event()
    install_signal_handlers(_Loop(), stop)  # type: ignore[arg-type]
    assert signal.SIGTERM in handlers
    handlers[signal.SIGTERM]()  # type: ignore[operator]
    assert stop.is_set()


async def test_reconcile_flags_a_foreign_position():
    send = ScriptedSend()
    rest = FakeREST(
        positions=[
            ["tETHF0:USTF0", "ACTIVE", 1.0, 2000.0, 0, 0, 0, 0, 0, 1, None, 1, 0, 0],
        ]
    )
    venue = BitfinexVenue(
        symbol=SYMBOL,
        market="perp",
        send=send,
        rest=rest,
        fees=FeeSchedule(0.0002, 0.0065),
        min_order_size=0.0004,
        max_order_size=1.0,
        max_stale_s=5.0,
    )
    problems = await venue.reconcile()
    assert any("tETHF0:USTF0" in problem for problem in problems)
    assert venue.api_errors >= 1
