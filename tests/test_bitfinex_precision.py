from __future__ import annotations

import pytest

from jev_trader.execution.base import OrderRejected
from jev_trader.execution.bitfinex.auth import Nonce, rest_sig_payload, sign
from jev_trader.execution.bitfinex.errors import scrub
from jev_trader.execution.bitfinex.messages import (
    FeeSchedule,
    flatten_body,
    parse_fee_schedule,
    parse_pair_spec,
    quote_body,
)
from jev_trader.execution.bitfinex.precision import (
    format_number,
    price_decimals,
    round_price,
    round_size,
)
from jev_trader.execution.bitfinex.symbols import market_for_symbol, split_symbol


def test_price_decimals_follow_significant_figures():
    assert price_decimals(98432) == 0
    assert price_decimals(3500.1) == 1
    assert price_decimals(100.12) == 2
    assert price_decimals(0.00012345) == 8


def test_passive_rounding_and_min_size():
    assert round_price(98432.7, side="buy") == 98432
    assert round_price(98432.2, side="sell") == 98433
    assert round_price(3500.19, side="buy") == 3500.1
    assert round_price(3500.11, side="sell") == 3500.2
    assert round_size(0.00049, min_size=0.0004) == 0.00049
    with pytest.raises(OrderRejected):
        round_size(0.00039, min_size=0.0004)
    assert format_number(98432.0) == "98432"
    assert "e" not in format_number(0.00012345)


def test_pair_spec_and_fees_parse_documented_shapes():
    payload = [
        [
            ["BTCF0:USTF0", [None, None, None, "0.0004", "2500.0", None, None, None, 0.01, 0.005]],
            ["ETHF0:USTF0", [None, None, None, "0.006", "5000", None]],
        ]
    ]
    spec = parse_pair_spec(payload, "tBTCF0:USTF0")
    assert spec is not None
    assert spec.min_order_size == 0.0004
    assert spec.max_order_size == 2500.0
    summary = [
        None,
        None,
        None,
        None,
        [
            [0.001, 0.001, 0.001, None, None, 0.0002],
            [0.002, 0.002, 0.002, None, None, 0.0065],
        ],
    ]
    perp = parse_fee_schedule(summary, market="perp")
    spot = parse_fee_schedule(summary, market="spot")
    assert perp == FeeSchedule(maker=-0.0002, taker=0.0065)
    assert spot == FeeSchedule(maker=0.001, taker=0.002)
    assert FeeSchedule.fallback("perp").maker > 0


def test_quote_and_flatten_flags():
    quote = quote_body(
        cid=7,
        symbol="tBTCF0:USTF0",
        market="perp",
        side="buy",
        price=98432.4,
        size=0.0004,
        leverage=1,
    )
    assert quote["flags"] == 4096
    assert quote["type"] == "LIMIT"
    assert quote["lev"] == 1
    assert quote["amount"] == "0.0004"
    assert quote["price"] == "98432"
    flatten = flatten_body(
        cid=8,
        symbol="tBTCF0:USTF0",
        market="perp",
        position=0.0004,
        size=0.0004,
        leverage=1,
    )
    assert flatten["flags"] == 1024
    assert flatten["type"] == "MARKET"
    assert str(flatten["amount"]).startswith("-")
    spot = flatten_body(
        cid=9,
        symbol="tBTCUSD",
        market="spot",
        position=0.01,
        size=0.01,
        leverage=None,
    )
    assert spot["type"] == "EXCHANGE MARKET"
    assert "flags" not in spot


def test_symbol_defaults_and_secrets_stay_out_of_the_signature():
    assert market_for_symbol("tBTCF0:USTF0") == "perp"
    assert market_for_symbol("tBTCUSD") == "spot"
    assert market_for_symbol("tBTCF0:USTF0", "spot") == "spot"
    assert split_symbol("tBTCF0:USTF0") == ("BTCF0", "USTF0")
    assert split_symbol("tETHUSD") == ("ETH", "USD")
    payload = rest_sig_payload("/v2/auth/r/wallets", "5", "{}")
    assert payload == "/api/v2/auth/r/wallets5{}"
    signature = sign("super-secret-value", payload)
    assert "super-secret-value" not in signature
    leaked = scrub("key super-secret-value leaked", ("super-secret-value",))
    assert "super-secret-value" not in leaked
    nonce = Nonce()
    first = int(nonce.next())
    second = int(nonce.next())
    assert second > first
