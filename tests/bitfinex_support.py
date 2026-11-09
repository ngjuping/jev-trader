from __future__ import annotations

from jev_trader.execution.bitfinex.venue import BitfinexVenue


def order_row(
    *,
    order_id: int,
    cid: int,
    symbol: str,
    amount: float,
    price: float,
    status: str = "ACTIVE",
    flags: int = 4096,
    amount_orig: float | None = None,
) -> list:
    row: list = [None] * 32
    row[0] = order_id
    row[2] = cid
    row[3] = symbol
    row[6] = amount
    row[7] = amount if amount_orig is None else amount_orig
    row[8] = "LIMIT"
    row[12] = flags
    row[13] = status
    row[16] = price
    return row


class FakeREST:
    def __init__(self, orders: list | None = None, positions: list | None = None) -> None:
        self.orders = orders or []
        self.positions = positions or []
        self.calls: list[tuple[str, object]] = []

    async def post_auth(self, path: str, body: dict | None = None) -> object:
        self.calls.append((path, body))
        if path.endswith("/r/orders"):
            return self.orders
        if path.endswith("/r/positions"):
            return self.positions
        if path.endswith("/r/wallets"):
            return []
        return [0, "on-req", None, None, None, None, "SUCCESS", "ok"]


class ScriptedSend:
    def __init__(self) -> None:
        self.sent: list = []
        self.venue: BitfinexVenue | None = None

    async def __call__(self, payload: object) -> None:
        self.sent.append(payload)
        if self.venue is not None:
            _ack(self.venue, payload)


def _ack(venue: BitfinexVenue, payload: object) -> None:
    if not isinstance(payload, list) or len(payload) < 4:
        return
    kind = payload[1]
    body = payload[3]
    if not isinstance(body, dict):
        return
    symbol = str(body.get("symbol") or venue.symbol)
    if kind == "on":
        cid = int(body["cid"])
        amount = float(body["amount"])
        price = float(body["price"]) if "price" in body else 0.0
        flags = int(body.get("flags") or 0)
        market = "MARKET" in str(body.get("type", ""))
        status = "EXECUTED" if market else "ACTIVE"
        row = order_row(
            order_id=cid,
            cid=cid,
            symbol=symbol,
            amount=0.0 if market else amount,
            price=price,
            status=status,
            flags=flags,
            amount_orig=amount,
        )
        venue.handle_message([0, "n", [1, "on-req", None, None, row, None, "SUCCESS", "ok"]])
        if market:
            trade = [
                cid,
                symbol,
                1_700_000_000_000,
                cid,
                amount,
                100.0,
                "MARKET",
                0,
                -1,
                -0.01,
                "USTF0",
                cid,
            ]
            venue.handle_message([0, "tu", trade])
        return
    if kind == "ou":
        order_id = int(body["id"])
        current = venue.account.orders.get(order_id)
        if "amount" in body:
            amount = float(body["amount"])
        elif current is not None:
            amount = current.remaining
        else:
            amount = 0.0
        if current is not None and amount > 0 and current.side == "sell":
            amount = -amount
        if "price" in body:
            price = float(body["price"])
        elif current is not None:
            price = current.price
        else:
            price = 0.0
        if current is None:
            cid = 0
            row_symbol = symbol
            flags = int(body.get("flags") or 4096)
        else:
            cid = current.cid or 0
            row_symbol = symbol if "symbol" in body else current.symbol
            flags = int(body.get("flags") or current.flags)
        row = order_row(
            order_id=order_id,
            cid=cid,
            symbol=row_symbol,
            amount=amount,
            price=price,
            flags=flags,
        )
        venue.handle_message([0, "ou", row])
        return
    if kind == "oc_multi":
        for order_id in body.get("id", []):
            current = venue.account.orders.get(int(order_id))
            if current is None:
                continue
            signed = current.remaining if current.side == "buy" else -current.remaining
            venue.handle_message(
                [
                    0,
                    "oc",
                    order_row(
                        order_id=current.id,
                        cid=current.cid or 0,
                        symbol=current.symbol,
                        amount=signed,
                        price=current.price,
                        status="CANCELED",
                        flags=current.flags,
                    ),
                ]
            )
