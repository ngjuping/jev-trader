"""GLFT / A-S quote formula unit tests."""
from __future__ import annotations

import math

import pytest

from jev_trader.config import PricingConfig, Settings
from jev_trader.pricing.avellaneda_stoikov import (
    compute_quote,
    glft_coefficients,
    optimal_half_spread,
    reservation_price,
)


# Live-like operating point (BTC ~73.3k, calibrated A/k, live gamma).
MID = 73_300.0
SIGMA = 3.665  # $/sqrt(s)  ~ median live realized_vol_short * mid
GAMMA = 0.004
A = 0.003204
K_REL = 1493.2
MIN_HALF = MID * 5.0 / 10_000.0
MAX_HALF = MID * 50.0 / 10_000.0


def _glft(**kwargs):
    params = dict(
        mid=MID,
        inventory=0.0,
        sigma=SIGMA,
        tau=60.0,
        gamma=GAMMA,
        kappa=100.0,
        min_half_spread=0.0,
        mode="glft",
        intensity_a=A,
        intensity_k_rel=K_REL,
        max_half_spread=MAX_HALF,
    )
    params.update(kwargs)
    return compute_quote(**params)


def test_symmetric_at_zero_inventory():
    q = _glft(inventory=0.0)
    assert q.reservation == pytest.approx(MID)
    assert q.bid == pytest.approx(MID - q.half_spread)
    assert q.ask == pytest.approx(MID + q.half_spread)
    assert q.delta_bid == pytest.approx(q.half_spread)
    assert q.delta_ask == pytest.approx(q.half_spread)
    assert q.mode == "glft"


def test_long_inventory_skews_quotes_down():
    z = _glft(inventory=0.0)
    long = _glft(inventory=1.0)
    short = _glft(inventory=-1.0)
    assert long.reservation < z.reservation < short.reservation
    assert long.bid < z.bid
    assert long.ask < z.ask
    assert short.bid > z.bid
    assert short.ask > z.ask
    # Long: more eager to sell (ask closer to mid) than to buy.
    assert (long.ask - MID) < (MID - long.bid)
    assert (short.ask - MID) > (MID - short.bid)


def test_half_spread_increases_with_sigma():
    low = _glft(sigma=1.0, min_half_spread=0.0)
    high = _glft(sigma=8.0, min_half_spread=0.0)
    assert 0.0 < low.half_spread < high.half_spread


def test_half_spread_and_skew_increase_with_gamma():
    prev_half = 0.0
    prev_psi = 0.0
    for gamma in (0.002, 0.004, 0.008, 0.016):
        q0 = _glft(gamma=gamma, inventory=0.0, min_half_spread=0.0)
        q1 = _glft(gamma=gamma, inventory=1.0, min_half_spread=0.0)
        psi = q0.reservation - q1.reservation  # mid - (mid - psi)
        assert q0.half_spread >= prev_half - 1e-9
        assert psi >= prev_psi - 1e-9
        prev_half = q0.half_spread
        prev_psi = psi


def test_min_half_spread_floor_applies():
    # Large k / zero vol makes the theoretical half sit below the 5 bp floor.
    q = _glft(sigma=0.0, intensity_k_rel=8000.0, min_half_spread=MIN_HALF)
    assert q.half_spread == pytest.approx(MIN_HALF)
    raw = _glft(sigma=0.0, intensity_k_rel=8000.0, min_half_spread=0.0)
    assert raw.half_spread < MIN_HALF


def test_max_half_spread_cap_applies():
    q = _glft(sigma=200.0, min_half_spread=MIN_HALF, max_half_spread=MAX_HALF)
    assert q.half_spread == pytest.approx(MAX_HALF)


def test_fixed_mode_matches_original_as_formula():
    q = compute_quote(
        mid=100.0,
        inventory=0.5,
        sigma=0.001,
        tau=60.0,
        gamma=0.5,
        kappa=1.5,
        min_half_spread=0.05,
        mode="fixed",
    )
    half = max(optimal_half_spread(0.5, 0.001, 60.0, 1.5), 0.05)
    res = reservation_price(100.0, 0.5, 0.5, 0.001, 60.0)
    assert q.half_spread == pytest.approx(half)
    assert q.reservation == pytest.approx(res)
    assert q.bid == pytest.approx(res - half)
    assert q.ask == pytest.approx(res + half)
    assert q.mode == "fixed"


def test_unknown_mode_does_not_crash_and_uses_as():
    q = compute_quote(
        mid=100.0,
        inventory=0.0,
        sigma=0.001,
        tau=60.0,
        gamma=0.5,
        kappa=1.5,
        min_half_spread=0.05,
        mode="not-a-real-mode",
    )
    assert q.bid < q.ask
    assert q.half_spread >= 0.05


def test_glft_coefficients_positive():
    k_price = K_REL / MID
    c1, c2 = glft_coefficients(GAMMA, A, k_price, lot=1.0)
    assert c1 > 0 and c2 > 0
    assert math.isfinite(c1) and math.isfinite(c2)


def test_env_overrides_for_glft(monkeypatch):
    monkeypatch.setenv("JEV_PRICING__QUOTE_MODE", "glft")
    monkeypatch.setenv("JEV_PRICING__INTENSITY_A", "0.003204")
    monkeypatch.setenv("JEV_PRICING__INTENSITY_K_REL", "1493.2")
    monkeypatch.setenv("JEV_PRICING__MAX_HALF_SPREAD_BPS", "50")
    monkeypatch.setenv("JEV_PRICING__MIN_HALF_SPREAD_BPS", "5")
    s = Settings()
    assert s.pricing.quote_mode == "glft"
    assert s.pricing.intensity_a == pytest.approx(0.003204)
    assert s.pricing.intensity_k_rel == pytest.approx(1493.2)
    assert s.pricing.max_half_spread_bps == 50.0
    assert s.pricing.min_half_spread_bps == 5.0


def test_pricing_config_defaults_keep_fixed_mode():
    p = PricingConfig()
    assert p.quote_mode == "fixed"
    assert p.max_half_spread_bps == 50.0
