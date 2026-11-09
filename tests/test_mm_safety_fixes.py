"""Regression: wider floor config reads; inventory gate on reduce-only path."""
from __future__ import annotations

from jev_trader.config import PricingConfig, Settings
from jev_trader.risk.limits import allowed_quote_sides


def test_min_half_spread_env_override_to_5(monkeypatch):
    monkeypatch.setenv("JEV_PRICING__MIN_HALF_SPREAD_BPS", "5")
    s = Settings()
    assert s.pricing.min_half_spread_bps == 5.0


def test_adopt_fill_race_scenario_blocks_increasing_side():
    # The live breach path: inv at 0.0004 with a resting buy 0.0002 still live.
    bid, ask = allowed_quote_sides(0.0004, 0.0004, open_buy=0.0002)
    assert bid is False and ask is True
    # After cancel of buy, still only ask until under cap.
    bid, ask = allowed_quote_sides(0.0004, 0.0004, open_buy=0.0)
    assert bid is False and ask is True
