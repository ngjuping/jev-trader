"""Hard inventory gate: projected |inv| never exceeds max_position."""
from __future__ import annotations

from jev_trader.risk.limits import allowed_quote_sides, clamp_order_size_for_cap


def test_at_cap_only_reducing_side():
    bid, ask = allowed_quote_sides(0.0004, 0.0004)
    assert bid is False and ask is True
    bid, ask = allowed_quote_sides(-0.0004, 0.0004)
    assert bid is True and ask is False


def test_over_cap_only_reducing_side():
    bid, ask = allowed_quote_sides(0.0006, 0.0004)
    assert bid is False and ask is True
    bid, ask = allowed_quote_sides(-0.0006, 0.0004)
    assert bid is True and ask is False


def test_open_buy_blocks_new_bid_when_projected_over_cap():
    # inv 0.0003 + resting buy 0.0002 = 0.0005 > 0.0004
    bid, ask = allowed_quote_sides(0.0003, 0.0004, open_buy=0.0002, open_sell=0.0)
    assert bid is False and ask is True


def test_open_sell_blocks_new_ask_when_projected_over_cap():
    bid, ask = allowed_quote_sides(-0.0003, 0.0004, open_buy=0.0, open_sell=0.0002)
    assert bid is True and ask is False


def test_clamp_buy_room():
    # inv 0.0003, room 0.0001
    assert abs(clamp_order_size_for_cap("buy", 0.0004, 0.0003, 0.0004) - 0.0001) < 1e-12


def test_clamp_sell_room():
    assert abs(clamp_order_size_for_cap("sell", 0.0004, -0.0003, 0.0004) - 0.0001) < 1e-12


def test_clamp_zero_when_no_room():
    assert clamp_order_size_for_cap("buy", 0.0002, 0.0004, 0.0004) == 0.0
