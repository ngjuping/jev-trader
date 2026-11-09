from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from jev_trader.execution.base import OrderRejected, VenueHealth
from jev_trader.execution.bitfinex.account import AccountBook, Ack, TrackedOrder
from jev_trader.execution.bitfinex.errors import BitfinexAPIError
from jev_trader.execution.bitfinex.messages import (
    FLAG_REDUCE_ONLY,
    FeeSchedule,
    RemoteOrder,
    flatten_body,
    is_live_status,
    parse_order_row,
    parse_positions,
    parse_wallets,
    quote_body,
    replace_body,
)
from jev_trader.execution.bitfinex.precision import round_price, round_size
from jev_trader.state.book import OrderBook
from jev_trader.state.events import BlockEvent
from jev_trader.types import Fill, Order, PnL

logger = logging.getLogger(__name__)

Send = Callable[[object], Awaitable[None]]


def is_order_reject(status: str) -> bool:
    """Venue refusals that are not transport failures and must not trip the kill switch."""
    upper = status.upper()
    return "POSTONLY" in upper or "INSUFFICIENT" in upper or upper.startswith("RSN_")


class BitfinexVenue:
    """Post-only Bitfinex adapter for one symbol on a dedicated sub-account.

    Orders go out on the authenticated websocket. REST is the reconciliation
    path and the fallback when the socket is down. `cancel_all` cancels resting
    quotes on this symbol and leaves an in-flight reduce-only flatten alone.
    """

    def __init__(
        self,
        *,
        symbol: str,
        market: str,
        send: Send,
        rest: Any,
        fees: FeeSchedule,
        min_order_size: float,
        max_order_size: float | None,
        max_stale_s: float,
        leverage: int | None = 1,
        ack_timeout_s: float = 5.0,
        dust: float = 1e-8,
        max_position: float | None = None,
    ) -> None:
        self.symbol = symbol
        self._market = market
        self._send = send
        self._rest = rest
        self._fees = fees
        self._min_order_size = min_order_size
        self._max_order_size = max_order_size
        self._max_stale_s = max_stale_s
        self._leverage = leverage if market == "perp" else None
        self._ack_timeout_s = ack_timeout_s
        self._dust = dust
        self._max_position = max_position
        self._orders_blind = False
        self._account = AccountBook(symbol)
        self._book: OrderBook | None = None
        self._book_ts = 0.0
        self._mid: float | None = None
        self._halted = False
        self._flattening = False
        self._cid = int(time.time() * 1000) % 1_000_000_000
        self._waiters_by_cid: dict[int, asyncio.Future[Ack]] = {}
        self._waiters_by_id: dict[int, asyncio.Future[Ack]] = {}
        self._changed = asyncio.Event()
        self._attempts = 0
        self._rejects = 0
        self._fill_count = 0
        self._reader: asyncio.Task[None] | None = None
        self._foreign: tuple[str, ...] = ()

    @property
    def api_errors(self) -> int:
        return self._account.api_errors

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def orders_blind(self) -> bool:
        """True after an order-ack or cancel timeout until quotes are flat again."""
        return self._orders_blind

    @property
    def fees(self) -> FeeSchedule:
        return self._fees

    @property
    def account(self) -> AccountBook:
        return self._account

    def adopt_remote(
        self,
        *,
        orders: tuple[RemoteOrder, ...],
        position_amount: float = 0.0,
        base_price: float = 0.0,
        opened_ts: float | None = None,
        quote_balance: float = 0.0,
    ) -> None:
        self._account.snap_position(position_amount, base_price, opened_ts)
        unrealized = self._account.unrealized(base_price if position_amount else None)
        self._account.set_opening_equity(quote_balance, unrealized)
        for order in orders:
            if order.symbol != self.symbol or not is_live_status(order.status):
                continue
            self._account.orders[order.id] = _tracked_from_remote(order)

    def on_block(self, event: BlockEvent) -> None:
        self._book = event.book
        self._book_ts = event.ts
        if event.book.two_sided:
            self._mid = event.book.mid

    def handle_message(self, message: object) -> None:
        acks = self._account.apply(message)
        for ack in acks:
            self._resolve(ack)
        self._changed.set()

    def drain_fills(self) -> tuple[Fill, ...]:
        fills = self._account.drain_fills()
        self._fill_count += len(fills)
        return fills

    def book(self) -> OrderBook:
        if self._book is None:
            raise RuntimeError("bitfinex venue has not seen a book yet")
        return self._book

    def inventory(self) -> float:
        return self._account.position

    def equity(self) -> float:
        return self._account.equity(self._mid)

    def pnl(self) -> PnL:
        return self._account.pnl(self._mid)

    def health(self) -> VenueHealth:
        if self._attempts == 0:
            return VenueHealth()
        return VenueHealth(
            fill_ratio=self._fill_count / self._attempts,
            reject_rate=self._rejects / self._attempts,
            slippage_bps=0.0,
        )

    def position_age_s(self, now: float) -> float:
        return self._account.position_age_s(now)

    def open_orders(self) -> tuple[Order, ...]:
        return tuple(
            Order(side=order.side, price=order.price, size=order.remaining, post_only=True)
            for order in self._account.orders.values()
            if order.flags & FLAG_REDUCE_ONLY == 0
        )

    async def cancel_all(self) -> int:
        if self._flattening:
            return 0
        ids = [
            order.id
            for order in self._account.orders.values()
            if order.flags & FLAG_REDUCE_ONLY == 0
        ]
        if not ids:
            return 0
        await self._cancel_ids(set(ids))
        return len(ids)

    async def place(self, order: Order) -> str:
        if self._halted:
            raise OrderRejected("halted")
        if self._orders_blind:
            raise OrderRejected("orders blind after timeout")
        try:
            self._require_fresh_book()
            normalized = self._normalize(order)
            self._reject_if_crossing(normalized)
            self._reject_if_inventory_breach(normalized)
        except OrderRejected:
            self._attempts += 1
            self._rejects += 1
            raise
        cid = self._next_cid()
        body = quote_body(
            cid=cid,
            symbol=self.symbol,
            market=self._market,
            side=normalized.side,
            price=normalized.price,
            size=normalized.size,
            leverage=self._leverage,
        )
        ack = await self._request([0, "on", None, body], cid=cid)
        if not ack.ok or ack.order_id is None or ack.order_id not in self._account.orders:
            self._rejects += 1
            if is_order_reject(ack.error):
                raise OrderRejected(ack.error or "order rejected")
            self._account.api_errors += 1
            raise BitfinexAPIError(ack.error or "order failed")
        return str(ack.order_id)

    async def replace(self, order_id: str, order: Order) -> str:
        if self._halted:
            raise OrderRejected("halted")
        if self._orders_blind:
            # Never replace after a timeout — pull only until synced.
            raise OrderRejected("orders blind after timeout")
        self._require_fresh_book()
        normalized = self._normalize(order)
        self._reject_if_crossing(normalized)
        self._reject_if_inventory_breach(normalized)
        body = replace_body(
            order_id=int(order_id),
            side=normalized.side,
            price=normalized.price,
            size=normalized.size,
            market=self._market,
            leverage=self._leverage,
        )
        ack = await self._request([0, "ou", None, body], order_id=int(order_id))
        if not ack.ok:
            self._rejects += 1
            if is_order_reject(ack.error):
                raise OrderRejected(ack.error or "replace rejected")
            self._account.api_errors += 1
            raise BitfinexAPIError(ack.error or "replace failed")
        return order_id

    async def sync_quotes(self, orders: tuple[Order, ...]) -> None:
        """Cancel/replace resting quotes so they match `orders`. Empty pulls everything."""
        if self._halted or self._book_is_stale():
            await self.cancel_all()
            return
        if self._orders_blind:
            # Blind mode: cancel both sides only. Never place/replace until flat.
            await self.cancel_all()
            if not self._account.orders:
                self._orders_blind = False
                logger.info("orders blind cleared; resting quotes flat")
            return
        desired: dict[str, Order] = {}
        for order in orders:
            try:
                desired[order.side] = self._normalize(order)
            except OrderRejected:
                self._rejects += 1
        for side, resting in list(self._quotes_by_side().items()):
            if side not in desired:
                await self._cancel_ids({order.id for order in resting})
                continue
            primary, *extra = resting
            if extra:
                await self._cancel_ids({order.id for order in extra})
            target = desired.pop(side)
            if _same_quote(primary, target):
                continue
            try:
                await self.replace(str(primary.id), target)
            except OrderRejected:
                await self._cancel_ids({primary.id})
        for target in desired.values():
            with contextlib.suppress(OrderRejected):
                await self.place(target)

    async def flatten(self) -> Fill | None:
        """Cancel every resting order, then reduce-only (perp) or exchange market (spot)."""
        self._halted = True
        self._flattening = True
        try:
            await self._cancel_remote_orders()
            qty = abs(self._account.position)
            if qty <= self._dust or qty < self._min_order_size:
                return None
            cid = self._next_cid()
            body = flatten_body(
                cid=cid,
                symbol=self.symbol,
                market=self._market,
                position=self._account.position,
                size=qty,
                leverage=self._leverage,
            )
            ack = await self._request([0, "on", None, body], cid=cid)
            if not ack.ok:
                self._account.api_errors += 1
                raise BitfinexAPIError(ack.error or "flatten rejected")
            await self._wait_until(
                lambda: abs(self._account.position) <= self._dust
                or abs(self._account.position) < self._min_order_size,
                self._ack_timeout_s,
            )
        except TimeoutError:
            self._account.api_errors += 1
            logger.error("flatten did not complete before the ack timeout")
        except BitfinexAPIError:
            logger.exception("flatten failed")
        except Exception:
            self._account.api_errors += 1
            logger.exception("flatten failed")
        fills = self.drain_fills()
        return fills[-1] if fills else None

    async def reconcile(self) -> tuple[str, ...]:
        """Snap wallets, positions, and orders. Foreign exposure is returned, not ignored."""
        from jev_trader.execution.bitfinex.guard import exposure_problems
        from jev_trader.execution.bitfinex.symbols import split_symbol

        positions = parse_positions(await self._rest.post_auth("/v2/auth/r/positions", {}))
        orders = _parse_orders(await self._rest.post_auth("/v2/auth/r/orders", {}))
        wallets = parse_wallets(await self._rest.post_auth("/v2/auth/r/wallets", {}))
        base, quote = split_symbol(self.symbol)
        problems = exposure_problems(
            symbol=self.symbol,
            positions=positions,
            orders=orders,
            wallets=wallets,
            base=base,
            quote=quote,
            market=self._market,
            adopt=True,
            dust=self._dust,
        )
        ours = [position for position in positions if position.symbol == self.symbol]
        if ours:
            current = ours[0]
            if abs(current.amount - self._account.position) > self._dust:
                logger.warning(
                    "position drift local=%.8g exchange=%.8g; snapping",
                    self._account.position,
                    current.amount,
                )
                self._account.snap_position(current.amount, current.base_price, current.opened_ts)
        elif abs(self._account.position) > self._dust:
            logger.warning("exchange reports no position; snapping local position to flat")
            self._account.snap_position(0.0, 0.0, None)
        remote_ids = {order.id for order in orders if order.symbol == self.symbol}
        for order in orders:
            if order.symbol == self.symbol and is_live_status(order.status):
                self._account.orders[order.id] = _tracked_from_remote(order)
        for order_id in list(self._account.orders):
            if order_id not in remote_ids:
                self._account.orders.pop(order_id, None)
        self._foreign = problems
        if problems:
            self._account.api_errors += 1
            logger.error("foreign exposure on sub-account: %s", "; ".join(problems))
        return problems

    def book_is_stale(self, now: float | None = None) -> bool:
        return self._book_is_stale(now)

    async def _cancel_remote_orders(self) -> None:
        try:
            payload = await self._rest.post_auth("/v2/auth/r/orders", {})
            remote = _parse_orders(payload)
        except Exception:
            logger.warning("order snapshot failed during flatten; cancelling known ids")
            remote = ()
            self._account.api_errors += 1
        for order in remote:
            if is_live_status(order.status):
                self._account.orders[order.id] = _tracked_from_remote(order)
        if self._account.orders:
            await self._cancel_ids(set(self._account.orders))

    async def _cancel_ids(self, ids: set[int]) -> None:
        pending = {order_id for order_id in ids if order_id in self._account.orders}
        if not pending:
            return
        await self._dispatch([0, "oc_multi", None, {"id": sorted(pending)}])
        try:
            await self._wait_until(
                lambda: all(order_id not in self._account.orders for order_id in pending),
                self._ack_timeout_s,
            )
        except TimeoutError:
            self._account.api_errors += 1
            self._orders_blind = True
            logger.error("cancel timed out; entering orders blind (no replace until flat)")
            raise BitfinexAPIError("cancel timed out") from None

    async def _request(
        self,
        payload: list,
        *,
        cid: int | None = None,
        order_id: int | None = None,
    ) -> Ack:
        self._attempts += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Ack] = loop.create_future()
        if cid is not None:
            self._waiters_by_cid[cid] = future
        if order_id is not None:
            self._waiters_by_id[order_id] = future
        try:
            await self._dispatch(payload)
            return await asyncio.wait_for(future, self._ack_timeout_s)
        except TimeoutError:
            self._account.api_errors += 1
            self._orders_blind = True
            logger.error("order ack timed out; entering orders blind and pulling quotes")
            # Best-effort cancel both sides; ignore nested failures while blind.
            with contextlib.suppress(Exception):
                await self._cancel_ids_best_effort(set(self._account.orders))
            raise BitfinexAPIError("order ack timed out") from None
        except BitfinexAPIError:
            self._account.api_errors += 1
            raise
        finally:
            if cid is not None and self._waiters_by_cid.get(cid) is future:
                self._waiters_by_cid.pop(cid, None)
            if order_id is not None and self._waiters_by_id.get(order_id) is future:
                self._waiters_by_id.pop(order_id, None)

    async def _dispatch(self, payload: list) -> None:
        try:
            await self._send(payload)
        except Exception:
            await self._rest_fallback(payload)

    async def _rest_fallback(self, payload: list) -> None:
        kind = payload[1] if len(payload) > 1 else ""
        body = payload[3] if len(payload) > 3 and isinstance(payload[3], dict) else {}
        paths = {
            "on": "/v2/auth/w/order/submit",
            "ou": "/v2/auth/w/order/update",
            "oc_multi": "/v2/auth/w/order/cancel/multi",
        }
        path = paths.get(str(kind))
        if path is None:
            self._account.api_errors += 1
            raise BitfinexAPIError("order send failed")
        logger.warning("bitfinex websocket send failed; using REST %s", path)
        note = await self._rest.post_auth(path, body)
        self.handle_message(note)

    def _resolve(self, ack: Ack) -> None:
        if ack.cid is not None:
            self._resolve_one(self._waiters_by_cid, ack.cid, ack)
        if ack.order_id is not None:
            self._resolve_one(self._waiters_by_id, ack.order_id, ack)

    def _resolve_one(
        self,
        waiters: dict[int, asyncio.Future[Ack]],
        key: int,
        ack: Ack,
    ) -> None:
        future = waiters.get(key)
        if future is not None and not future.done():
            future.set_result(ack)

    async def _wait_until(self, predicate: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._changed.clear()
            if predicate():
                return
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                if predicate():
                    return
                raise

    def _require_fresh_book(self) -> None:
        if self._book is None or not self._book.two_sided:
            raise OrderRejected("book is not two-sided")
        if self._book_is_stale():
            raise OrderRejected("stale book")

    def _book_is_stale(self, now: float | None = None) -> bool:
        if self._book_ts <= 0:
            return True
        stamp = time.time() if now is None else now
        return stamp - self._book_ts > self._max_stale_s

    def _normalize(self, order: Order) -> Order:
        price = round_price(order.price, side=order.side)
        size = round_size(
            order.size,
            min_size=self._min_order_size,
            max_size=self._max_order_size,
        )
        return Order(side=order.side, price=price, size=size, post_only=True)

    def _reject_if_inventory_breach(self, order: Order) -> None:
        """Hard cap: projected inventory including this order must stay within max_position."""
        if self._max_position is None or self._max_position <= 0:
            return
        inv = self._account.position
        open_buy = sum(
            o.remaining for o in self._account.orders.values()
            if o.flags & FLAG_REDUCE_ONLY == 0 and o.side == "buy"
        )
        open_sell = sum(
            o.remaining for o in self._account.orders.values()
            if o.flags & FLAG_REDUCE_ONLY == 0 and o.side == "sell"
        )
        if order.side == "buy":
            projected = inv + open_buy + order.size
        else:
            projected = inv - open_sell - order.size
        if abs(projected) > self._max_position + 1e-12:
            raise OrderRejected(
                f"projected inventory {projected:.8g} exceeds max_position {self._max_position}"
            )

    async def _cancel_ids_best_effort(self, ids: set[int]) -> None:
        """Fire cancel without waiting for ack (used after an ack timeout)."""
        pending = {order_id for order_id in ids if order_id in self._account.orders}
        if not pending:
            return
        with contextlib.suppress(Exception):
            await self._dispatch([0, "oc_multi", None, {"id": sorted(pending)}])

    def _reject_if_crossing(self, order: Order) -> None:
        book = self._book
        if book is None or not book.two_sided:
            raise OrderRejected("book is not two-sided")
        if order.side == "buy" and order.price >= book.best_ask.price:
            raise OrderRejected("post-only buy would cross the book")
        if order.side == "sell" and order.price <= book.best_bid.price:
            raise OrderRejected("post-only sell would cross the book")

    def _quotes_by_side(self) -> dict[str, list[Any]]:
        grouped: dict[str, list[Any]] = {}
        for order in self._account.orders.values():
            if order.flags & FLAG_REDUCE_ONLY:
                continue
            grouped.setdefault(order.side, []).append(order)
        return grouped

    def _next_cid(self) -> int:
        self._cid += 1
        return self._cid


def _parse_orders(payload: object) -> tuple[RemoteOrder, ...]:
    if not isinstance(payload, list):
        return ()
    orders: list[RemoteOrder] = []
    for row in payload:
        parsed = parse_order_row(row)
        if parsed is not None:
            orders.append(parsed)
    return tuple(orders)


def _tracked_from_remote(order: RemoteOrder) -> TrackedOrder:
    return TrackedOrder(
        id=order.id,
        cid=order.cid,
        symbol=order.symbol,
        side=order.side,
        price=order.price,
        remaining=order.remaining,
        status=order.status,
        flags=order.flags,
    )


def _same_quote(resting: Any, target: Order) -> bool:
    return (
        resting.side == target.side
        and abs(resting.price - target.price) <= 1e-9
        and abs(resting.remaining - target.size) <= 1e-8
    )
