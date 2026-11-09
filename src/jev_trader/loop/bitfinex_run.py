from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from pathlib import Path

from jev_trader.calibration.fills import FillsLedger
from jev_trader.calibration.log import CalibrationLogger
from jev_trader.config import Settings
from jev_trader.execution.bitfinex.errors import BitfinexAPIError, ForeignExposure
from jev_trader.execution.bitfinex.guard import assert_account_clear, funding_error
from jev_trader.execution.bitfinex.messages import (
    FeeSchedule,
    RemoteOrder,
    parse_fee_schedule,
    parse_order_row,
    parse_pair_spec,
    parse_positions,
    parse_wallets,
)
from jev_trader.execution.bitfinex.precision import price_quantum
from jev_trader.execution.bitfinex.rest import BitfinexREST
from jev_trader.execution.bitfinex.stream import connect_websocket, run_private_session
from jev_trader.execution.bitfinex.symbols import market_for_symbol, split_symbol
from jev_trader.execution.bitfinex.venue import BitfinexVenue
from jev_trader.execution.paper import PaperVenue
from jev_trader.feeds.bitfinex import BitfinexFeed
from jev_trader.judgment.client import JevClient
from jev_trader.judgment.fallback import HeuristicJudge
from jev_trader.loop.engine import TradingLoop
from jev_trader.types import Order

logger = logging.getLogger(__name__)


class _SocketHub:
    """Holds the current private socket. Payloads are JSON-encoded by the socket."""

    def __init__(self) -> None:
        self.ws: object | None = None

    def set_socket(self, ws: object | None) -> None:
        self.ws = ws

    async def send(self, payload: object) -> None:
        ws = self.ws
        if ws is None:
            raise BitfinexAPIError("private socket is down")
        await ws.send(payload)  # type: ignore[attr-defined]


class ShadowVenue(PaperVenue):
    """Paper fills on a live book, with Bitfinex price and size increments."""

    def __init__(self, *, min_order_size: float, **kwargs: float) -> None:
        super().__init__(**kwargs)
        self._min_order_size = min_order_size

    async def place(self, order: Order) -> str:
        from jev_trader.execution.bitfinex.precision import round_price, round_size

        price = round_price(order.price, side=order.side)
        size = round_size(
            order.size,
            min_size=self._min_order_size,
            max_size=self._max_order_size,
        )
        return await super().place(
            Order(side=order.side, price=price, size=size, post_only=order.post_only)
        )


def apply_max_notional(settings: Settings, max_notional: float | None) -> Settings:
    if max_notional is None:
        return settings
    if max_notional <= 0:
        raise ValueError("max-notional must be positive")
    risk = settings.risk.model_copy(update={"max_notional": max_notional})
    return settings.model_copy(update={"risk": risk})


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _stop() -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _stop)


def parse_orders_payload(payload: object) -> tuple[RemoteOrder, ...]:
    if not isinstance(payload, list):
        return ()
    orders: list[RemoteOrder] = []
    for row in payload:
        parsed = parse_order_row(row)
        if parsed is not None:
            orders.append(parsed)
    return tuple(orders)


def mid_from_book(payload: object) -> float | None:
    if not isinstance(payload, list):
        return None
    bids: list[float] = []
    asks: list[float] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 3:
            continue
        price = float(row[0])
        amount = float(row[2])
        if amount > 0:
            bids.append(price)
        elif amount < 0:
            asks.append(price)
    if not bids or not asks:
        return None
    return (max(bids) + min(asks)) / 2.0


def _quote_balance(wallets: tuple, *, market: str, quote: str) -> float:
    wallet_type = "margin" if market == "perp" else "exchange"
    return sum(
        wallet.balance
        for wallet in wallets
        if wallet.wallet_type == wallet_type and wallet.currency == quote
    )


async def run_bitfinex(
    settings: Settings,
    *,
    mode: str,
    max_notional: float | None,
    symbol: str | None,
    adopt: bool,
    blocks: int | None,
    log_path: Path | None,
    fills_path: Path | None,
) -> int:
    """Shadow (real book, paper fills) or live (real orders on the sub-account)."""
    if mode == "live" and (max_notional is None or max_notional <= 0):
        print("live mode requires a positive --max-notional cap")
        return 2
    if mode == "live" and adopt:
        print("live --adopt takes over existing orders and the position on this symbol only")
    try:
        settings = apply_max_notional(settings, max_notional)
    except ValueError as exc:
        print(exc)
        return 2

    symbol = symbol or settings.bfx.symbol
    market = market_for_symbol(symbol, settings.bfx.market or None)
    keys = settings.subaccount_keys()
    if mode == "live" and keys is None:
        print(
            "BFX_SUB_API_KEY and BFX_SUB_API_SECRET are required for live mode. "
            "Use the dedicated sub-account, never the main account."
        )
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    api_key, api_secret = keys if keys is not None else ("", "")
    rest = BitfinexREST(
        api_key=api_key,
        api_secret=api_secret,
        rest_url=settings.bfx.rest_url,
        public_rest_url=settings.bfx.public_rest_url,
    )
    async with rest:
        prepared = await _prepare(
            rest,
            settings=settings,
            symbol=symbol,
            market=market,
            mode=mode,
            adopt=adopt,
        )
        if isinstance(prepared, int):
            return prepared
        spec_min, exchange_max, fees, mid, positions, orders, quote_balance = prepared
        return await _trade(
            rest,
            settings=settings,
            symbol=symbol,
            market=market,
            mode=mode,
            adopt=adopt,
            blocks=blocks,
            log_path=log_path,
            fills_path=fills_path,
            min_order_size=spec_min,
            exchange_max_size=exchange_max,
            fees=fees,
            mid=mid,
            positions=positions,
            orders=orders,
            quote_balance=quote_balance,
            api_key=api_key,
            api_secret=api_secret,
        )


async def _prepare(
    rest: BitfinexREST,
    *,
    settings: Settings,
    symbol: str,
    market: str,
    mode: str,
    adopt: bool,
) -> tuple | int:
    conf_key = "pub:info:pair:futures" if market == "perp" else "pub:info:pair"
    try:
        conf = await rest.get_public(f"/v2/conf/{conf_key}")
        book = await rest.get_public(f"/v2/book/{symbol}/P0?len=1")
    except BitfinexAPIError as exc:
        print(f"bitfinex public data failed: {exc}")
        return 1
    spec = parse_pair_spec(conf, symbol)
    min_size = settings.bfx.min_order_size
    exchange_max = spec.max_order_size if spec is not None else None
    if spec is not None and min_size is None:
        min_size = spec.min_order_size
    if min_size is None or min_size <= 0:
        print(f"no minimum order size for {symbol} in {conf_key}")
        return 1
    mid = mid_from_book(book)
    if mid is None:
        print(f"no two-sided book for {symbol}")
        return 1
    if settings.risk.max_notional > 0 and min_size * mid > settings.risk.max_notional:
        print(
            f"min order notional {min_size * mid:.2f} exceeds "
            f"--max-notional {settings.risk.max_notional:.2f}"
        )
        return 2

    fees = FeeSchedule.fallback(market)
    positions: tuple = ()
    orders: tuple = ()
    wallets: tuple = ()
    if mode == "live":
        try:
            summary = await rest.post_auth("/v2/auth/r/summary", {})
            wallets = parse_wallets(await rest.post_auth("/v2/auth/r/wallets", {}))
            positions = parse_positions(await rest.post_auth("/v2/auth/r/positions", {}))
            orders = parse_orders_payload(await rest.post_auth("/v2/auth/r/orders", {}))
        except BitfinexAPIError as exc:
            print(f"bitfinex account read failed: {exc}")
            return 1
        parsed = parse_fee_schedule(summary, market=market)
        if parsed is not None:
            fees = parsed
    if settings.bfx.maker_fee_rate is not None or settings.bfx.taker_fee_rate is not None:
        fees = FeeSchedule(
            maker=(
                settings.bfx.maker_fee_rate
                if settings.bfx.maker_fee_rate is not None
                else fees.maker
            ),
            taker=(
                settings.bfx.taker_fee_rate
                if settings.bfx.taker_fee_rate is not None
                else fees.taker
            ),
        )
    base, quote = split_symbol(symbol)
    if mode == "live":
        funded = funding_error(wallets, market=market, quote=quote)
        if funded:
            print(funded)
            return 2
        try:
            assert_account_clear(
                symbol=symbol,
                positions=positions,
                orders=orders,
                wallets=wallets,
                base=base,
                quote=quote,
                market=market,
                adopt=adopt,
            )
        except ForeignExposure as exc:
            print("refusing to start; this sub-account is not empty:")
            for reason in exc.reasons:
                print(f"  - {reason}")
            print("pass --adopt only to take over this symbol. other symbols are never adopted.")
            return 2
    quote_balance = _quote_balance(wallets, market=market, quote=quote)
    print(
        f"{mode}: symbol={symbol} market={market} mid={mid:.4f} "
        f"min_size={min_size} max_notional={settings.risk.max_notional or 'unset'} "
        f"leverage={settings.bfx.leverage if market == 'perp' else 'n/a'} "
        f"maker_fee={fees.maker:.6f} taker_fee={fees.taker:.6f}"
    )
    if mode == "live":
        print(f"quote balance {quote_balance:.4f} {quote}")
    return min_size, exchange_max, fees, mid, positions, orders, quote_balance


async def _trade(
    rest: BitfinexREST,
    *,
    settings: Settings,
    symbol: str,
    market: str,
    mode: str,
    adopt: bool,
    blocks: int | None,
    log_path: Path | None,
    fills_path: Path | None,
    min_order_size: float,
    exchange_max_size: float | None,
    fees: FeeSchedule,
    mid: float,
    positions: tuple,
    orders: tuple,
    quote_balance: float,
    api_key: str,
    api_secret: str,
) -> int:
    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop)
    resolved_log = log_path if log_path is not None else settings.log_path
    resolved_fills = fills_path if fills_path is not None else settings.fills_path
    calibration = CalibrationLogger(resolved_log)
    fills = FillsLedger(resolved_fills)
    fallback = HeuristicJudge(max_position=settings.risk.max_position)
    max_exchange_size = settings.risk.max_order_size
    if exchange_max_size is not None:
        max_exchange_size = min(max_exchange_size, exchange_max_size)
    if mode == "shadow":
        venue: PaperVenue | BitfinexVenue = ShadowVenue(
            min_order_size=min_order_size,
            starting_cash=settings.starting_cash,
            fee_bps=fees.maker * 10_000.0,
            tick_size=price_quantum(mid),
            max_order_size=max_exchange_size,
        )
    else:
        hub = _SocketHub()
        venue = BitfinexVenue(
            symbol=symbol,
            market=market,
            send=hub.send,
            rest=rest,
            fees=fees,
            min_order_size=min_order_size,
            max_order_size=max_exchange_size,
            max_stale_s=settings.risk.max_stale_data_age_s,
            leverage=settings.bfx.leverage,
            ack_timeout_s=settings.bfx.order_ack_timeout_s,
        )
        _seed_live_venue(
            venue,
            symbol=symbol,
            adopt=adopt,
            positions=positions,
            orders=orders,
            quote_balance=quote_balance,
            mid=mid,
        )

    async def _on_blind() -> None:
        with contextlib.suppress(Exception):
            await venue.cancel_all()

    async def _on_blind_timeout() -> None:
        logger.error("book blind for %.0fs; stopping", settings.bfx.max_blind_s)
        if mode == "live":
            await venue.flatten()
        stop.set()

    feed = BitfinexFeed(
        symbol=symbol,
        block_ms=settings.block_ms,
        connect=lambda: connect_websocket(settings.bfx.public_ws_url),
        stop=stop,
        on_blind=_on_blind,
        on_blind_timeout=_on_blind_timeout,
        max_blind_s=settings.bfx.max_blind_s,
        max_blocks=blocks,
    )
    private: asyncio.Task[None] | None = None
    reconcile: asyncio.Task[None] | None = None
    if mode == "live":
        assert isinstance(venue, BitfinexVenue)
        private = asyncio.create_task(
            run_private_session(
                connect=lambda: connect_websocket(settings.bfx.private_ws_url),
                stop=stop,
                api_key=api_key,
                api_secret=api_secret,
                on_message=venue.handle_message,
                on_api_error=lambda: setattr(venue.account, "api_errors", venue.api_errors + 1),
                on_socket=hub.set_socket,
            )
        )
        reconcile = asyncio.create_task(
            _reconcile_loop(venue, settings.bfx.reconcile_interval_s, stop)
        )

    loop = TradingLoop(
        feed=feed,
        venue=venue,
        judge=fallback,
        fallback=fallback,
        settings=settings,
        calibration=calibration,
        fills=fills,
    )
    try:
        async with contextlib.AsyncExitStack() as stack:
            if settings.has_api_key:
                loop._judge = await stack.enter_async_context(JevClient(settings))
            stats = await loop.run()
    finally:
        stop.set()
        if mode == "live":
            with contextlib.suppress(Exception):
                await venue.flatten()
        for task in (private, reconcile):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        calibration.close()
        fills.close()
    print(json.dumps(stats.summary(), indent=2))
    print(f"calibration log: {resolved_log}")
    print(f"fills log: {resolved_fills}")
    return 0


def _seed_live_venue(
    venue: BitfinexVenue,
    *,
    symbol: str,
    adopt: bool,
    positions: tuple,
    orders: tuple,
    quote_balance: float,
    mid: float,
) -> None:
    adopted = [position for position in positions if position.symbol == symbol]
    amount = adopted[0].amount if adopt and adopted else 0.0
    base_price = adopted[0].base_price if adopt and adopted else 0.0
    opened = adopted[0].opened_ts if adopt and adopted else None
    ours = tuple(order for order in orders if adopt and order.symbol == symbol)
    venue.adopt_remote(
        orders=ours,
        position_amount=amount,
        base_price=base_price,
        opened_ts=opened,
        quote_balance=quote_balance,
    )
    if amount == 0:
        venue.account.set_opening_equity(quote_balance, 0.0)
    else:
        venue.account.set_opening_equity(quote_balance, (mid - base_price) * amount)


async def _reconcile_loop(venue: BitfinexVenue, interval: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            problems = await venue.reconcile()
        except Exception:
            logger.warning("reconcile failed", exc_info=True)
            venue.account.api_errors += 1
            continue
        if problems:
            logger.error("kill switch: foreign exposure during reconcile")
            await venue.flatten()
            stop.set()
            return
