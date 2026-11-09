from __future__ import annotations

import pytest

from jev_trader.execution.bitfinex.errors import ForeignExposure
from jev_trader.execution.bitfinex.guard import (
    assert_account_clear,
    exposure_problems,
    funding_error,
)
from jev_trader.execution.bitfinex.messages import PositionView, RemoteOrder, WalletBalance


def _position(symbol: str, amount: float) -> PositionView:
    return PositionView(symbol, "ACTIVE", amount, 100.0, 0.0, None)


def _order(symbol: str, order_id: int = 1) -> RemoteOrder:
    return RemoteOrder(order_id, 5, symbol, 0.01, 100.0, "ACTIVE", 4096)


def _wallet(currency: str, balance: float, wallet_type: str = "margin") -> WalletBalance:
    return WalletBalance(wallet_type, currency, balance, balance)


def test_refuses_foreign_position_and_order_without_adopt():
    with pytest.raises(ForeignExposure) as raised:
        assert_account_clear(
            symbol="tBTCF0:USTF0",
            positions=(_position("tBTCF0:USTF0", 0.01),),
            orders=(),
            adopt=False,
        )
    assert "tBTCF0:USTF0" in str(raised.value)
    with pytest.raises(ForeignExposure):
        assert_account_clear(
            symbol="tBTCF0:USTF0",
            positions=(),
            orders=(_order("tETHF0:USTF0"),),
            adopt=False,
        )


def test_adopt_keeps_this_symbol_and_still_refuses_others():
    assert (
        exposure_problems(
            symbol="tBTCF0:USTF0",
            positions=(_position("tBTCF0:USTF0", 0.01),),
            orders=(_order("tBTCF0:USTF0"),),
            adopt=True,
        )
        == ()
    )
    problems = exposure_problems(
        symbol="tBTCF0:USTF0",
        positions=(
            _position("tBTCF0:USTF0", 0.01),
            _position("tETHF0:USTF0", 1.0),
        ),
        orders=(),
        adopt=True,
    )
    assert len(problems) == 1
    assert "tETHF0:USTF0" in problems[0]


def test_unexpected_balance_and_unfunded_wallet():
    problems = exposure_problems(
        symbol="tBTCF0:USTF0",
        positions=(),
        orders=(),
        wallets=(_wallet("ETH", 1.5),),
        quote="USTF0",
        market="perp",
    )
    assert problems
    assert "ETH" in problems[0]
    assert (
        funding_error((_wallet("USTF0", 0.0),), market="perp", quote="USTF0")
        is not None
    )
    assert funding_error((_wallet("USTF0", 200.0),), market="perp", quote="USTF0") is None
    assert (
        funding_error((), market="spot", quote="USD") is not None
    )
