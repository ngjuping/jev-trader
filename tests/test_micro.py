from __future__ import annotations

import pytest

from jev_trader.config import PricingConfig, RiskConfig, Settings
from jev_trader.execution.paper import PaperVenue
from jev_trader.feeds.synthetic import SyntheticFeed
from jev_trader.judgment.fallback import HeuristicJudge
from jev_trader.loop.engine import TradingLoop
from jev_trader.state.book import Level, OrderBook
from jev_trader.state.events import BlockEvent, Trade
from jev_trader.state.micro import MicroSignalEngine, combine_signal, ofi_increment
from jev_trader.types import Action, ActionKind


def book(bid: float, bid_size: float, ask: float, ask_size: float, ts: float = 0.0) -> OrderBook:
    return OrderBook(ts=ts, bids=(Level(bid, bid_size),), asks=(Level(ask, ask_size),))


def event(b: OrderBook, ts: float, trades: tuple[Trade, ...] = ()) -> BlockEvent:
    return BlockEvent(block=int(ts * 10), ts=ts, book=b, trades=trades)


# --- OFI -------------------------------------------------------------------

def test_ofi_same_prices_is_size_change():
    prev = book(100.0, 5.0, 100.1, 5.0)
    cur = book(100.0, 8.0, 100.1, 2.0)
    # bid +3 (buy pressure), ask -3 consumed (buy pressure)
    assert ofi_increment(prev, cur) == pytest.approx(6.0)


def test_ofi_bid_up_tick_counts_full_new_size():
    prev = book(100.0, 5.0, 100.2, 5.0)
    cur = book(100.1, 2.0, 100.2, 5.0)
    assert ofi_increment(prev, cur) == pytest.approx(2.0)


def test_ofi_bid_down_tick_is_sell_pressure():
    prev = book(100.0, 5.0, 100.2, 5.0)
    cur = book(99.9, 7.0, 100.2, 5.0)
    assert ofi_increment(prev, cur) == pytest.approx(-5.0)


def test_ofi_ask_down_tick_is_sell_pressure():
    prev = book(100.0, 5.0, 100.2, 5.0)
    cur = book(100.0, 5.0, 100.1, 4.0)
    assert ofi_increment(prev, cur) == pytest.approx(-4.0)


def test_ofi_window_rolls_and_normalizes():
    eng = MicroSignalEngine(ofi_window_s=1.0, jump_bps=0.0)
    eng.update(event(book(100.0, 5.0, 100.1, 5.0), 0.0))
    s1 = eng.update(event(book(100.0, 9.0, 100.1, 5.0), 0.3))
    assert s1.ofi == pytest.approx(4.0)
    assert s1.ofi_norm > 0
    # far later: window has rolled, no new flow
    s2 = eng.update(event(book(100.0, 9.0, 100.1, 5.0), 5.0))
    assert s2.ofi == pytest.approx(0.0)


# --- signal ----------------------------------------------------------------

def test_combine_signal_bounded_and_signed():
    assert combine_signal(100.0, 1.0, w_ofi=0.5, w_obi=0.5) == pytest.approx(1.0)
    assert combine_signal(-100.0, -1.0, w_ofi=0.5, w_obi=0.5) == pytest.approx(-1.0)
    assert combine_signal(0.0, 0.0, w_ofi=0.5, w_obi=0.5) == 0.0
    assert -1.0 <= combine_signal(3.0, -0.2, w_ofi=0.7, w_obi=0.3) <= 1.0
    assert combine_signal(1.0, 1.0, w_ofi=0.0, w_obi=0.0) == 0.0


def test_signal_negative_on_sell_pressure():
    eng = MicroSignalEngine(jump_bps=0.0)
    eng.update(event(book(100.0, 5.0, 100.1, 5.0), 0.0))
    sig = eng.update(event(book(100.0, 0.5, 100.1, 20.0), 0.3))
    assert sig.obi < 0
    assert sig.ofi < 0
    assert sig.signal < -0.6


# --- toxicity --------------------------------------------------------------

def test_toxic_pause_on_big_trade_then_expires():
    eng = MicroSignalEngine(trade_min_samples=10, pause_s=1.5, jump_bps=0.0)
    b = book(100.0, 5.0, 100.1, 5.0)
    for i in range(20):
        sig = eng.update(event(b, i * 0.3, (Trade(i * 0.3, 100.0, 0.01, "buy"),)))
        assert not sig.toxic
    t = 20 * 0.3
    sig = eng.update(event(b, t, (Trade(t, 100.0, 1.0, "sell"),)))
    assert sig.toxic and "big_trade" in sig.toxic_reason
    assert sig.paused(t + 1.0)
    assert not sig.paused(t + 1.6)


def test_toxic_pause_on_mid_jump():
    eng = MicroSignalEngine(jump_bps=2.0, pause_s=1.0)
    eng.update(event(book(100.0, 5.0, 100.01, 5.0), 0.0))
    sig = eng.update(event(book(100.05, 5.0, 100.06, 5.0), 0.3))  # ~5 bp
    assert sig.toxic and "jump" in sig.toxic_reason
    sig2 = eng.update(event(book(100.05, 5.0, 100.06, 5.0), 0.6))
    assert not sig2.toxic and sig2.paused(0.6)


# --- engine quoting --------------------------------------------------------

def make_loop(**pricing):
    settings = Settings(
        risk=RiskConfig(max_position=1.0, max_order_size=0.5),
        pricing=PricingConfig(**pricing),
    )
    venue = PaperVenue(starting_cash=1000.0, fee_bps=0.0, tick_size=0.01, max_order_size=0.5)
    fallback = HeuristicJudge(max_position=1.0)
    feed = SyntheticFeed(seed=1, block_ms=300, max_blocks=1)
    return TradingLoop(feed=feed, venue=venue, judge=fallback, fallback=fallback, settings=settings)


def prime(loop: TradingLoop, books: list[OrderBook]) -> None:
    for i, b in enumerate(books):
        e = event(b, i * 0.3)
        loop._micro_signal = loop._micro.update(e)
        loop._last_book = b


QUOTE = Action(ActionKind.QUOTE_BOTH_SIDES)


def test_one_sided_pull_drops_bid_on_sell_pressure(make_state):
    loop = make_loop(toxic_jump_bps=0.0, one_sided_threshold=0.6)
    prime(loop, [book(100.0, 5.0, 100.1, 5.0), book(100.0, 0.5, 100.1, 20.0)])
    assert loop._micro_signal.signal < -0.6
    orders = loop._quote_orders(QUOTE, make_state(ts=0.3, mid=100.05, microprice=100.0), False)
    assert [o.side for o in orders] == ["sell"]
    assert loop._stats.micro_one_sided_pulls == 1


def test_one_sided_pull_drops_ask_on_buy_pressure(make_state):
    loop = make_loop(toxic_jump_bps=0.0, one_sided_threshold=0.6)
    prime(loop, [book(100.0, 5.0, 100.1, 5.0), book(100.0, 20.0, 100.1, 0.5)])
    orders = loop._quote_orders(QUOTE, make_state(ts=0.3, mid=100.05, microprice=100.1), False)
    assert [o.side for o in orders] == ["buy"]


def test_reduce_only_keeps_reducing_side_when_pull_hits_other_side(make_state):
    loop = make_loop(toxic_jump_bps=0.0, one_sided_threshold=0.6)
    prime(loop, [book(100.0, 5.0, 100.1, 5.0), book(100.0, 0.5, 100.1, 20.0)])
    # long + reduce-only: only the ask is wanted; sell pressure pulls the bid, ask stays
    orders = loop._quote_orders(
        QUOTE, make_state(ts=0.3, mid=100.05, microprice=100.0, inventory=0.5), True
    )
    assert [o.side for o in orders] == ["sell"]


def test_balanced_book_quotes_both_sides_around_microprice(make_state):
    loop = make_loop(toxic_jump_bps=0.0, min_half_spread_bps=3.0, gamma=0.0001)
    b = book(100.0, 5.0, 100.1, 5.0)
    prime(loop, [b, b])
    state = make_state(ts=0.3, mid=100.05, microprice=100.05, realized_vol_short=0.0)
    orders = loop._quote_orders(QUOTE, state, False)
    assert sorted(o.side for o in orders) == ["buy", "sell"]
    bid = next(o for o in orders if o.side == "buy").price
    ask = next(o for o in orders if o.side == "sell").price
    assert bid <= 100.0 and ask >= 100.1  # never through the touch
    assert loop._stats.micro_one_sided_pulls == 0


def test_signal_shifts_center_toward_prediction(make_state):
    base = make_loop(toxic_jump_bps=0.0, one_sided_threshold=0.0, micro_alpha_bps=0.0,
                     clamp_to_touch=False, gamma=0.0001)
    shifted = make_loop(toxic_jump_bps=0.0, one_sided_threshold=0.0, micro_alpha_bps=5.0,
                        clamp_to_touch=False, gamma=0.0001)
    books = [book(100.0, 5.0, 100.1, 5.0), book(100.0, 15.0, 100.1, 5.0)]
    prime(base, books)
    prime(shifted, books)
    state = make_state(ts=0.3, mid=100.05, microprice=100.075, realized_vol_short=0.0)
    q0 = {o.side: o.price for o in base._quote_orders(QUOTE, state, False)}
    q1 = {o.side: o.price for o in shifted._quote_orders(QUOTE, state, False)}
    assert q1["buy"] > q0["buy"] and q1["sell"] > q0["sell"]


def test_toxic_pause_pulls_both_sides(make_state):
    loop = make_loop(toxic_jump_bps=2.0, toxic_pause_s=1.5)
    prime(loop, [book(100.0, 5.0, 100.01, 5.0), book(100.05, 5.0, 100.06, 5.0)])
    assert loop._micro_signal.toxic
    assert loop._quote_orders(QUOTE, make_state(ts=0.3, mid=100.055), False) == []
    assert loop._quote_orders(QUOTE, make_state(ts=1.0, mid=100.055), False) == []
    assert loop._stats.micro_toxic_pauses == 2
    assert loop._quote_orders(QUOTE, make_state(ts=2.0, mid=100.055), False) != []


def test_env_overrides_pricing_micro(monkeypatch):
    monkeypatch.setenv("JEV_PRICING__MICRO_ALPHA_BPS", "1.0")
    monkeypatch.setenv("JEV_PRICING__ONE_SIDED_THRESHOLD", "0.7")
    monkeypatch.setenv("JEV_PRICING__TOXIC_PAUSE_S", "2")
    s = Settings()
    assert s.pricing.micro_alpha_bps == 1.0
    assert s.pricing.one_sided_threshold == 0.7
    assert s.pricing.toxic_pause_s == 2.0
    assert s.pricing.use_microprice is True
