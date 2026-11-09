from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from jev_trader.execution.bitfinex.market import MarketSession
from jev_trader.execution.bitfinex.stream import Connect, Sleep, run_public_session
from jev_trader.state.events import BlockEvent

BlindHook = Callable[[], Awaitable[None]]


class BitfinexFeed:
    """Real Bitfinex book and trades, sampled on `block_ms` like the paper block clock.

    No block is emitted until the P0 book is synced. While the socket is blind the
    optional `on_blind` hook runs (cancel resting quotes). If the blind stretch
    exceeds `max_blind_s`, `on_blind_timeout` runs and the feed stops.
    """

    def __init__(
        self,
        *,
        symbol: str,
        block_ms: int,
        connect: Connect,
        stop: asyncio.Event,
        session: MarketSession | None = None,
        on_blind: BlindHook | None = None,
        on_blind_timeout: BlindHook | None = None,
        max_blind_s: float = 30.0,
        max_blocks: int | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._session = session or MarketSession(symbol)
        self._block_ms = block_ms
        self._connect = connect
        self._stop = stop
        self._on_blind = on_blind
        self._on_blind_timeout = on_blind_timeout
        self._max_blind_s = max_blind_s
        self._max_blocks = max_blocks
        self._sleep = sleep

    @property
    def session(self) -> MarketSession:
        return self._session

    async def _wait_block(self) -> bool:
        """Sleep one block. Return True when stop was requested."""
        sleep_task = asyncio.create_task(self._sleep(self._block_ms / 1000.0))
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {sleep_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return stop_task in done

    async def events(self) -> AsyncIterator[BlockEvent]:
        reader = asyncio.create_task(
            run_public_session(self._session, self._connect, self._stop, sleep=self._sleep)
        )
        block = 0
        try:
            while not self._stop.is_set():
                if await self._wait_block():
                    break
                event = self._session.block_event(block)
                if event is None:
                    if self._on_blind is not None:
                        await self._on_blind()
                    if self._session.blind_for() > self._max_blind_s:
                        if self._on_blind_timeout is not None:
                            await self._on_blind_timeout()
                        break
                    continue
                yield event
                block += 1
                if self._max_blocks is not None and block >= self._max_blocks:
                    break
        finally:
            self._stop.set()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


def block_from_session(session: MarketSession, block: int) -> BlockEvent | None:
    """Build one block from an already-updated session. Used by tests and shadow."""
    return session.block_event(block)
