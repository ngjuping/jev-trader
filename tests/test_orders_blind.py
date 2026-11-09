"""Ack/cancel timeout enters orders-blind: pull only, never replace until flat."""
from __future__ import annotations

import asyncio
import time

import pytest
from tests.bitfinex_support import FakeREST, ScriptedSend, order_row

from jev_trader.execution.base import OrderRejected
from jev_trader.execution.bitfinex.errors import BitfinexAPIError
from jev_trader.execution.bitfinex.messages import FeeSchedule
from jev_trader.execution.bitfinex.venue import BitfinexVenue
from jev_trader.state.book import Level, OrderBook
from jev_trader.state.events import BlockEvent
from jev_trader.types import Order

SYMBOL = "tBTCF0:USTF0"


class SilentSend:
    """Send that never produces an ack — forces order-ack timeout."""

    def __init__(self) -> None:
        self.sent: list = []

    async def __call__(self, payload: object) -> None:
        self.sent.append(payload)


def _book_venue(send, *, max_position: float | None = 0.0004, ack_timeout_s: float = 0.05) -> BitfinexVenue:
    venue = BitfinexVenue(
        symbol=SYMBOL,
        market="perp",
        send=send,
        rest=FakeREST(),
        fees=FeeSchedule(0.0002, 0.0065),
        min_order_size=0.00005,
        max_order_size=1.0,
        max_stale_s=5.0,
        leverage=1,
        ack_timeout_s=ack_timeout_s,
        max_position=max_position,
    )
    if hasattr(send, "venue"):
        send.venue = venue
    book = OrderBook(ts=time.time(), bids=(Level(99.0, 1.0),), asks=(Level(101.0, 1.0),))
    venue.on_block(BlockEvent(block=0, ts=time.time(), book=book))
    return venue


@pytest.mark.asyncio
async def test_ack_timeout_enters_blind_and_blocks_replace():
    send = SilentSend()
    venue = _book_venue(send)
    with pytest.raises(BitfinexAPIError, match="order ack timed out"):
        await venue.place(Order(side="buy", price=99.0, size=0.0001))
    assert venue.orders_blind is True
    with pytest.raises(OrderRejected, match="orders blind"):
        await venue.replace("1", Order(side="buy", price=98.0, size=0.0001))


@pytest.mark.asyncio
async def test_blind_sync_quotes_only_cancels_never_places():
    # Seed a resting order, enter blind, sync with desired quotes — must cancel only.
    send = ScriptedSend()
    venue = _book_venue(send, ack_timeout_s=1.0)
    send.venue = venue
    oid = await venue.place(Order(side="buy", price=99.0, size=0.0001))
    assert venue.open_orders()
    venue._orders_blind = True  # simulate prior timeout
    before = len(send.sent)
    await venue.sync_quotes(
        (Order(side="buy", price=99.0, size=0.0001), Order(side="sell", price=101.0, size=0.0001))
    )
    kinds = [p[1] for p in send.sent[before:]]
    assert "on" not in kinds and "ou" not in kinds
    assert "oc_multi" in kinds
    # after successful cancel of resting, blind clears
    assert venue.orders_blind is False
    assert venue.open_orders() == ()


@pytest.mark.asyncio
async def test_cancel_timeout_enters_blind():
    class CancelHang(ScriptedSend):
        async def __call__(self, payload: object) -> None:
            self.sent.append(payload)
            if isinstance(payload, list) and payload[1] == "oc_multi":
                return  # no ack -> cancel timeout
            await super().__call__(payload)

    send = CancelHang()
    venue = _book_venue(send, ack_timeout_s=0.05)
    send.venue = venue
    await venue.place(Order(side="buy", price=99.0, size=0.0001))
    with pytest.raises(BitfinexAPIError, match="cancel timed out"):
        await venue.cancel_all()
    assert venue.orders_blind is True


@pytest.mark.asyncio
async def test_place_rejects_when_projected_inventory_breaches_cap():
    send = ScriptedSend()
    venue = _book_venue(send, max_position=0.0004, ack_timeout_s=1.0)
    send.venue = venue
    venue.account.snap_position(0.00035, 100.0, time.time())
    with pytest.raises(OrderRejected, match="projected inventory"):
        await venue.place(Order(side="buy", price=99.0, size=0.0002))
