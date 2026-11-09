from __future__ import annotations

import pytest

from jev_trader.cli import _build_parser, main
from jev_trader.config import RiskConfig, Settings
from jev_trader.loop.bitfinex_run import apply_max_notional


def test_live_requires_max_notional():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["live"])


def test_shadow_allows_a_missing_notional_cap():
    args = _build_parser().parse_args(["shadow", "--symbol", "tETHF0:USTF0"])
    assert args.max_notional is None
    assert args.symbol == "tETHF0:USTF0"


def test_live_refuses_to_start_without_subaccount_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BFX_SUB_API_KEY", raising=False)
    monkeypatch.delenv("BFX_SUB_API_SECRET", raising=False)
    monkeypatch.setenv("BFX_SUB_API_KEY", "")
    monkeypatch.setenv("BFX_SUB_API_SECRET", "")
    assert main(["live", "--max-notional", "50"]) == 2


def test_apply_max_notional_overrides_risk_without_dropping_other_limits():
    settings = apply_max_notional(Settings(risk=RiskConfig(max_daily_loss=10)), 50)
    assert settings.risk.max_notional == 50
    assert settings.risk.max_daily_loss == 10
    with pytest.raises(ValueError):
        apply_max_notional(Settings(), 0)
