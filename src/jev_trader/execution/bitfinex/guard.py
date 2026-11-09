from __future__ import annotations

from jev_trader.execution.bitfinex.errors import ForeignExposure
from jev_trader.execution.bitfinex.messages import PositionView, RemoteOrder, WalletBalance

# Collateral we expect a dedicated quoting sub-account to hold. Anything else
# is treated as exposure this process does not own.
_COLLATERAL = {"USD", "UST", "USDT", "USTF0", "USDF0"}


def exposure_problems(
    *,
    symbol: str,
    positions: tuple[PositionView, ...],
    orders: tuple[RemoteOrder, ...],
    wallets: tuple[WalletBalance, ...] = (),
    base: str | None = None,
    quote: str | None = None,
    market: str = "perp",
    adopt: bool = False,
    dust: float = 1e-10,
) -> tuple[str, ...]:
    """Reasons the process must refuse to trade. Empty means the account is clear.

    Without `--adopt`, any position or order is foreign: this process did not
    place it. With `--adopt`, only the configured symbol may be taken over.
    Other symbols still refuse, so a key pointed at the wrong account stops here.
    """
    problems: list[str] = []
    for position in positions:
        if abs(position.amount) <= dust:
            continue
        if position.status and position.status.upper() not in {"ACTIVE", ""}:
            continue
        if adopt and position.symbol == symbol:
            continue
        problems.append(
            f"position {position.symbol} amount {position.amount:.8g} is not owned by this bot"
        )
    for order in orders:
        if adopt and order.symbol == symbol:
            continue
        problems.append(f"open order {order.id} on {order.symbol} is not owned by this bot")
    allowed = set(_COLLATERAL)
    if quote:
        allowed.add(quote)
    if market == "spot" and base:
        allowed.add(base)
    for wallet in wallets:
        if wallet.currency in allowed:
            continue
        if abs(wallet.balance) <= dust:
            continue
        problems.append(
            f"unexpected {wallet.wallet_type} balance in {wallet.currency} "
            f"({wallet.balance:.8g})"
        )
    return tuple(problems)


def assert_account_clear(
    *,
    symbol: str,
    positions: tuple[PositionView, ...],
    orders: tuple[RemoteOrder, ...],
    wallets: tuple[WalletBalance, ...] = (),
    base: str | None = None,
    quote: str | None = None,
    market: str = "perp",
    adopt: bool = False,
    dust: float = 1e-10,
) -> None:
    problems = exposure_problems(
        symbol=symbol,
        positions=positions,
        orders=orders,
        wallets=wallets,
        base=base,
        quote=quote,
        market=market,
        adopt=adopt,
        dust=dust,
    )
    if problems:
        raise ForeignExposure(problems)


def funding_error(
    wallets: tuple[WalletBalance, ...],
    *,
    market: str,
    quote: str,
) -> str | None:
    """Return an error when the quoting wallet has no collateral."""
    wallet_type = "margin" if market == "perp" else "exchange"
    matches = [
        wallet
        for wallet in wallets
        if wallet.wallet_type == wallet_type and wallet.currency == quote
    ]
    if not matches:
        return f"no {wallet_type} wallet in {quote}; fund the sub-account before live"
    balance = sum(wallet.balance for wallet in matches)
    if balance <= 0:
        return f"{wallet_type} wallet {quote} balance is {balance}; fund the sub-account"
    return None
