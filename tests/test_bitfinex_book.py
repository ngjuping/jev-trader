from __future__ import annotations

import asyncio

from jev_trader.execution.bitfinex.book import BitfinexBook, signed_crc32
from jev_trader.execution.bitfinex.market import (
    MarketSession,
    control_action,
    subscribe_messages,
)
from jev_trader.execution.bitfinex.stream import run_public_session
from jev_trader.execution.paper import PaperVenue
from jev_trader.feeds.bitfinex import BitfinexFeed
from jev_trader.types import Order


def test_snapshot_and_update_keep_bids_and_asks_sorted():
    book = BitfinexBook()
    kind = book.apply(
        [
            [100.0, 2, 1.5],
            [101.0, 1, -2.0],
            [99.0, 1, 0.5],
            [102.0, 3, -0.4],
        ]
    )
    assert kind == "snapshot"
    view = book.to_order_book(1.0)
    assert [level.price for level in view.bids] == [100.0, 99.0]
    assert [level.price for level in view.asks] == [101.0, 102.0]
    assert view.bids[0].size == 1.5
    assert view.asks[0].size == 2.0

    assert book.apply([99.0, 0, 1]) == "update"
    assert book.apply([101.0, 4, -3.0]) == "update"
    view = book.to_order_book(2.0)
    assert [level.price for level in view.bids] == [100.0]
    assert view.asks[0].size == 3.0


def test_checksum_matches_signed_crc_and_a_mismatch_unsyncs():
    book = BitfinexBook()
    book.apply([[100.5, 1, 1.25], [101.5, 1, -2.0]])
    payload = book.checksum_payload()
    assert payload == "100.5:1.25:101.5:-2"
    checksum = signed_crc32(payload)
    assert book.verify_checksum(checksum)
    assert book.synced
    assert not book.verify_checksum(checksum + 1)
    assert not book.synced


def test_public_trade_te_is_kept_and_snapshot_is_not():
    session = MarketSession("tBTCF0:USTF0")
    session.handle({"event": "subscribed", "channel": "trades", "chanId": 7})
    session.handle({"event": "subscribed", "channel": "book", "chanId": 3})
    session.handle([3, [[100.0, 1, 1.0], [101.0, 1, -1.0]]])
    session.handle([7, [[1, 1_700_000_000_000, 0.5, 100.0]]])
    session.handle([7, "te", [9, 1_700_000_000_100, -0.2, 100.0]])
    session.handle([7, "tu", [9, 1_700_000_000_100, -0.2, 100.0]])
    event = session.block_event(0)
    assert event is not None
    assert len(event.trades) == 1
    assert event.trades[0].side == "sell"
    assert event.trades[0].size == 0.2
    assert event.book.mid == 100.5


def test_control_actions():
    assert control_action({"event": "info", "code": 20051}) == "reconnect"
    assert control_action({"event": "info", "code": 20060}) == "pause"
    assert control_action({"event": "info", "code": 20061}) == "resume"
    assert control_action({"event": "info", "platform": {"status": 0}}) == "pause"
    assert control_action({"event": "subscribed"}) is None


def test_subscribe_requests_p0_len_25_and_checksum():
    conf, book, trades = subscribe_messages("tBTCF0:USTF0")
    assert conf == {"event": "conf", "flags": 131072}
    assert book["channel"] == "book"
    assert book["prec"] == "P0"
    assert book["len"] == "25"
    assert book["symbol"] == "tBTCF0:USTF0"
    assert trades["channel"] == "trades"


class _ScriptedSocket:
    def __init__(self, messages: list, sent: list) -> None:
        self._messages = list(messages)
        self._sent = sent
        self._closed = asyncio.Event()

    async def __aenter__(self) -> _ScriptedSocket:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, payload: object) -> None:
        self._sent.append(payload)

    async def recv(self) -> object:
        if self._messages:
            return self._messages.pop(0)
        await self._closed.wait()
        return None

    def close(self) -> None:
        self._closed.set()


async def test_checksum_mismatch_resubscribes_from_a_fresh_snapshot():
    sent: list = []
    sockets: list[_ScriptedSocket] = []
    bad = _ScriptedSocket(
        [
            {"event": "subscribed", "channel": "book", "chanId": 1},
            [1, [[50.0, 1, 1.0], [51.0, 1, -1.0]]],
            [1, "cs", 1],
        ],
        sent,
    )
    good_book = BitfinexBook()
    good_book.apply([[100.0, 1, 2.0], [101.0, 1, -2.0]])
    good = _ScriptedSocket(
        [
            {"event": "subscribed", "channel": "book", "chanId": 2},
            [2, [[100.0, 1, 2.0], [101.0, 1, -2.0]]],
            [2, "cs", signed_crc32(good_book.checksum_payload())],
        ],
        sent,
    )
    sockets.extend([bad, good])

    def connect() -> _ScriptedSocket:
        return sockets.pop(0)

    session = MarketSession("tBTCF0:USTF0")
    stop = asyncio.Event()

    async def _sleep(_seconds: float) -> None:
        return None

    task = asyncio.create_task(
        run_public_session(session, connect, stop, sleep=_sleep)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if session.synced and session.book.to_order_book(0).best_bid.price == 100.0:
            break
    stop.set()
    bad.close()
    good.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert session.synced
    assert session.book.to_order_book(0).best_bid.price == 100.0
    assert any(message.get("prec") == "P0" for message in sent if isinstance(message, dict))
    assert session.api_errors == 0


async def test_stop_interrupts_the_block_wait():
    stop = asyncio.Event()

    class _Hanging:
        async def __aenter__(self) -> _Hanging:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def send(self, payload: object) -> None:
            return None

        async def recv(self) -> object:
            await asyncio.Event().wait()
            return None

    feed = BitfinexFeed(
        symbol="tBTCF0:USTF0",
        block_ms=60_000,
        connect=lambda: _Hanging(),
        stop=stop,
    )

    async def _consume() -> int:
        count = 0
        async for _event in feed.events():
            count += 1
        return count

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    stop.set()
    assert await asyncio.wait_for(task, timeout=1) == 0


async def test_live_book_prints_fill_the_paper_venue():
    """Shadow path: a public trade through a resting quote fills the paper venue."""
    session = MarketSession("tBTCUSD")
    session.handle({"event": "subscribed", "channel": "book", "chanId": 1})
    session.handle({"event": "subscribed", "channel": "trades", "chanId": 2})
    session.handle([1, [[99.0, 1, 1.0], [101.0, 1, -1.0]]])
    first = session.block_event(0)
    assert first is not None
    venue = PaperVenue(starting_cash=1_000.0, fee_bps=2.0, tick_size=1.0, max_order_size=1.0)
    venue.on_block(first)
    await venue.place(Order(side="buy", price=99.0, size=0.2))
    session.handle([2, "te", [4, 1_700_000_000_000, -0.2, 98.0]])
    second = session.block_event(1)
    assert second is not None
    venue.on_block(second)
    fills = venue.drain_fills()
    assert len(fills) == 1
    assert fills[0].size == 0.2
    assert venue.inventory() == 0.2
    assert venue.pnl().fees > 0
