from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jev_trader.execution.bitfinex.market import (
    MarketSession,
    control_action,
    decode_message,
    subscribe_messages,
)

logger = logging.getLogger(__name__)

# Returns an async context manager yielding an object with send() and recv().
Connect = Callable[[], Any]
Sleep = Callable[[float], Awaitable[None]]


async def run_public_session(
    session: MarketSession,
    connect: Connect,
    stop: asyncio.Event,
    *,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Keep the public book subscribed. Any break resubscribes from a fresh snapshot."""
    backoff = 1.0
    while not stop.is_set():
        try:
            async with connect() as ws:
                session.reset()
                for message in subscribe_messages(session.symbol):
                    await ws.send(message)
                backoff = 1.0
                while not stop.is_set():
                    raw = await ws.recv()
                    if raw is None:
                        break
                    message = decode_message(raw)
                    action = control_action(message)
                    if action in {"reconnect", "pause"}:
                        logger.info("bitfinex public stream %s", action)
                        session.mark_unsynced()
                        break
                    session.handle(message)
                    if session.needs_resubscribe:
                        session.needs_resubscribe = False
                        logger.info("bitfinex book checksum failed; resubscribing")
                        session.mark_unsynced()
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("bitfinex public stream disconnected", exc_info=True)
            session.note_api_error()
            session.mark_unsynced()
        if stop.is_set():
            break
        await sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)


def connect_websocket(url: str) -> Any:
    """Async context manager around a websocket. `send` accepts JSON-able objects."""
    import json

    import websockets

    class _Socket:
        def __init__(self) -> None:
            self._ws: Any = None

        async def __aenter__(self) -> _Socket:
            self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=20)
            return self

        async def __aexit__(self, *exc: object) -> None:
            if self._ws is not None:
                await self._ws.close()

        async def send(self, payload: object) -> None:
            data = payload if isinstance(payload, str) else json.dumps(payload)
            await self._ws.send(data)

        async def recv(self) -> object:
            return await self._ws.recv()

    return _Socket()


async def run_private_session(
    *,
    connect: Connect,
    stop: asyncio.Event,
    api_key: str,
    api_secret: str,
    on_message: Callable[[object], None],
    on_api_error: Callable[[], None],
    on_socket: Callable[[Any], None] | None = None,
    sleep: Sleep = asyncio.sleep,
    nonce_factory: Callable[[], str] | None = None,
) -> None:
    """Authenticated socket. Auth payloads are sent and never logged."""
    from jev_trader.execution.bitfinex.auth import Nonce, ws_auth_message

    nonce = Nonce()
    next_nonce = nonce_factory or nonce.next
    backoff = 1.0
    while not stop.is_set():
        try:
            async with connect() as ws:
                if on_socket is not None:
                    on_socket(ws)
                try:
                    await ws.send(ws_auth_message(api_key, api_secret, next_nonce()))
                    backoff = 1.0
                    while not stop.is_set():
                        raw = await ws.recv()
                        if raw is None:
                            break
                        message = decode_message(raw)
                        action = control_action(message)
                        if action in {"reconnect", "pause"}:
                            logger.info("bitfinex private stream %s", action)
                            break
                        if (
                            isinstance(message, dict)
                            and message.get("event") == "auth"
                            and message.get("status") != "OK"
                        ):
                            logger.error("bitfinex private auth rejected")
                            on_api_error()
                            break
                        on_message(message)
                finally:
                    if on_socket is not None:
                        on_socket(None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("bitfinex private stream disconnected", exc_info=True)
            on_api_error()
        if stop.is_set():
            break
        await sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)
