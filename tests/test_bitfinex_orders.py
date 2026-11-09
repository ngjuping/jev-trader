from __future__ import annotations

import time

import pytest
from tests.bitfinex_support import FakeREST, ScriptedSend, order_row

from jev_trader.execution.base import OrderRejected
from jev_trader.execution.bitfinex.account import AccountBook
from jev_trader.execution.bitfinex.messages import FeeSchedule
from jev_trader.execution.bitfinex.venue import BitfinexVenue
from jev_trader.state.book import Level, OrderBook
from jev_trader.state.events import BlockEvent
from jev_trader.types import Order

SYMBOL = "tBTCF0:USTF0"


def _venue(send: ScriptedSend, *, min_size: float = 0.0004) -> BitfinexVenue:
    venue = BitfinexVenue(
        symbol=SYMBOL,
        market="perp",
        send=send,
        rest=FakeREST(),
        fees=FeeSchedule(0.0002, 0.0065),
        min_order_size=min_size,
        max_order_size=1.0,
        max_stale_s=5.0,
        leverage=1,
        ack_timeout_s=1.0,
    )
    send.venue = venue
    book = OrderBook(
        ts=time.time(),
        bids=(Level(99.0, 1.0),),
        asks=(Level(101.0, 1.0),),
    )
    venue.on_block(BlockEvent(block=0, ts=time.time(), book=book))
    return venue


def _trade(
    trade_id: int,
    amount: float,
    price: float,
    order_id: int,
    fee: float = -0.01,
) -> list:
    row = [
        trade_id,
        SYMBOL,
        1_700_000_000_000,
        order_id,
        amount,
        price,
        "LIMIT",
        price,
        1,
        fee,
        "USTF0",
        1,
    ]
    return [0, "tu", row]


async def test_post_only_place_replace_and_cancel():
    send = ScriptedSend()
    venue = _venue(send)
    order_id = await venue.place(Order(side="buy", price=99.4, size=0.01))
    assert order_id
    submitted = send.sent[0][3]
    assert submitted["flags"] == 4096
    assert submitted["type"] == "LIMIT"
    assert submitted["lev"] == 1
    assert submitted["meta"] == {"protect_selfmatch": 1}
    assert float(submitted["price"]) <= 99.4

    await venue.replace(order_id, Order(side="buy", price=98.2, size=0.01))
    replaced = send.sent[1][3]
    assert replaced["id"] == int(order_id)
    assert replaced["flags"] == 4096
    assert venue.open_orders()[0].price == float(replaced["price"])

    cancelled = await venue.cancel_all()
    assert cancelled == 1
    assert send.sent[2][1] == "oc_multi"
    assert venue.open_orders() == ()


async def test_partial_fill_then_completion_updates_position_once():
    book = AccountBook(SYMBOL)
    opened = order_row(order_id=10, cid=3, symbol=SYMBOL, amount=0.01, price=99.0, amount_orig=0.01)
    book.apply([0, "on", opened])
    book.apply(_trade(1, 0.004, 99.0, 10, fee=-0.002))
    book.apply(
        [
            0,
            "ou",
            order_row(
                order_id=10,
                cid=3,
                symbol=SYMBOL,
                amount=0.006,
                price=99.0,
                status="PARTIALLY FILLED @ 99.0(0.004)",
                amount_orig=0.01,
            ),
        ]
    )
    assert book.position == pytest.approx(0.004)
    assert book.orders[10].remaining == pytest.approx(0.006)
    assert book.fees == pytest.approx(0.002)
    fills = book.drain_fills()
    assert len(fills) == 1
    assert fills[0].size == pytest.approx(0.004)

    book.apply(_trade(1, 0.004, 99.0, 10, fee=-0.002))
    assert book.position == pytest.approx(0.004)
    book.apply(_trade(2, 0.006, 99.0, 10, fee=-0.003))
    book.apply(
        [
            0,
            "oc",
            order_row(
                order_id=10,
                cid=3,
                symbol=SYMBOL,
                amount=0.0,
                price=99.0,
                status="EXECUTED @ 99.0(0.01)",
                amount_orig=0.01,
            ),
        ]
    )
    assert book.position == pytest.approx(0.01)
    assert book.orders == {}
    assert book.pnl(99.0).fees == pytest.approx(0.005)


async def test_post_only_reject_and_size_below_minimum():
    send = ScriptedSend()
    venue = _venue(send)
    with pytest.raises(OrderRejected):
        await venue.place(Order(side="buy", price=101.0, size=0.01))
    assert send.sent == []
    with pytest.raises(OrderRejected):
        await venue.place(Order(side="buy", price=99.0, size=0.0001))


async def test_sync_quotes_replaces_one_side_and_places_the_other():
    send = ScriptedSend()
    venue = _venue(send)
    await venue.place(Order(side="buy", price=99.0, size=0.01))
    await venue.sync_quotes(
        (
            Order(side="buy", price=98.0, size=0.01),
            Order(side="sell", price=102.0, size=0.01),
        )
    )
    kinds = [message[1] for message in send.sent]
    assert "ou" in kinds
    assert kinds.count("on") == 2
    sides = {order.side for order in venue.open_orders()}
    assert sides == {"buy", "sell"}


async def test_stale_book_does_not_quote():
    send = ScriptedSend()
    venue = _venue(send)
    venue.on_block(
        BlockEvent(
            block=1,
            ts=time.time() - 30,
            book=venue.book(),
        )
    )
    with pytest.raises(OrderRejected, match="stale"):
        await venue.place(Order(side="buy", price=99.0, size=0.01))
    await venue.sync_quotes((Order(side="buy", price=99.0, size=0.01),))
    assert all(message[1] != "on" for message in send.sent)
