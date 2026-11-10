from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quote:
    reservation: float
    bid: float
    ask: float
    half_spread: float
    delta_bid: float = 0.0
    delta_ask: float = 0.0
    sigma: float = 0.0
    gamma: float = 0.0
    intensity_a: float = 0.0
    intensity_k: float = 0.0
    inventory: float = 0.0
    mode: str = "fixed"


def reservation_price(
    mid: float,
    inventory: float,
    gamma: float,
    sigma: float,
    tau: float,
) -> float:
    """A-S reservation price: mid - q * gamma * sigma^2 * tau.

    `inventory` is normalized to [-1, 1] by max position, `sigma` is per-second
    volatility, `tau` is the remaining horizon in seconds.
    """
    return mid - inventory * gamma * sigma * sigma * tau


def optimal_half_spread(gamma: float, sigma: float, tau: float, kappa: float) -> float:
    """A-S optimal half spread: gamma * sigma^2 * tau + (2/gamma) * ln(1 + gamma/kappa)."""
    return gamma * sigma * sigma * tau + (2.0 / gamma) * math.log1p(gamma / kappa)


def glft_coefficients(
    gamma: float,
    intensity_a: float,
    intensity_k: float,
    lot: float = 1.0,
) -> tuple[float, float]:
    """GLFT 2013 closed-form coefficients, eqs. (4.6)–(4.7) with ξ = γ and Δ = lot.

    c1 = 1/(γΔ) * ln(1 + γΔ/k)
    c2 = sqrt( γ / (2 A Δ k) * (1 + γΔ/k)^{k/(γΔ) + 1} )

    Then half_spread = c1 + (Δ/2) * σ * c2 and reservation = mid - q * σ * c2.
    `intensity_k` is the k in λ(δ) = A * exp(-k δ) with δ in the same price
    units as σ (dollars per sqrt-second). Inventory q is in units of `lot`
    (we use lot=1 after normalizing inventory by max_position).
    """
    g = max(float(gamma), 1e-12)
    a = max(float(intensity_a), 1e-12)
    k = max(float(intensity_k), 1e-12)
    delta = max(float(lot), 1e-12)
    x = g * delta / k
    c1 = math.log1p(x) / (g * delta)
    # lim x->0 (1+x)^{1/x} = e
    if x < 1e-9:
        power = math.e * (1.0 + x)
    else:
        power = (1.0 + x) ** (1.0 / x + 1.0)
    c2 = math.sqrt((g / (2.0 * a * delta * k)) * power)
    return c1, c2


def _apply_tick(bid: float, ask: float, tick_size: float) -> tuple[float, float]:
    if tick_size > 0:
        bid = math.floor(bid / tick_size) * tick_size
        ask = math.ceil(ask / tick_size) * tick_size
    return bid, ask


def compute_quote(
    *,
    mid: float,
    inventory: float,
    sigma: float,
    tau: float,
    gamma: float,
    kappa: float,
    min_half_spread: float,
    skew: float = 0.0,
    half_spread_multiplier: float = 1.0,
    tick_size: float = 0.0,
    mode: str = "fixed",
    intensity_a: float = 0.003204,
    intensity_k_rel: float = 1493.2,
    max_half_spread: float = 0.0,
) -> Quote:
    """Compute a two-sided quote around a reservation price.

    mode:
      * "fixed" / "as" — original Avellaneda–Stoikov reservation and half-spread,
        then the min_half_spread floor (live behaviour before GLFT).
      * "glft" — Guéant–Lehalle–Fernandez-Tapia asymptotic closed form.
        λ(δ) = A * exp(-k_rel * δ/mid); k_price = k_rel / mid.

    Calibration of A and k_rel (2026-11-11, post 4e2218d deploy on jev-mm):
      135 maker fills over 67144.9s, quoting 62.76% of decisions
      -> per-side fill rate λ = 0.0016018 /s at median quoted depth 4.642 bp.
      The book is almost always quoted at that single depth (the 5 bp floor
      binds for both normal and wide actions), so k is not separately
      identified. We set k_rel so intensity halves when depth doubles from
      that median: k_rel = ln(2) / 4.642e-4 = 1493.2, then
      A = λ * exp(k_rel * 4.642e-4) = 0.003204 /s.
    """
    normalized = (mode or "fixed").strip().lower()
    if normalized == "glft":
        return _compute_glft(
            mid=mid,
            inventory=inventory,
            sigma=sigma,
            gamma=gamma,
            intensity_a=intensity_a,
            intensity_k_rel=intensity_k_rel,
            min_half_spread=min_half_spread,
            max_half_spread=max_half_spread,
            skew=skew,
            half_spread_multiplier=half_spread_multiplier,
            tick_size=tick_size,
        )
    half_spread = max(
        optimal_half_spread(gamma, sigma, tau, kappa) * half_spread_multiplier,
        min_half_spread,
    )
    reservation = reservation_price(mid, inventory, gamma, sigma, tau) + skew * half_spread
    bid = reservation - half_spread
    ask = reservation + half_spread
    bid, ask = _apply_tick(bid, ask, tick_size)
    return Quote(
        reservation=reservation,
        bid=bid,
        ask=ask,
        half_spread=half_spread,
        delta_bid=mid - bid,
        delta_ask=ask - mid,
        sigma=sigma,
        gamma=gamma,
        intensity_a=0.0,
        intensity_k=kappa,
        inventory=inventory,
        mode="fixed" if normalized not in {"as", "fixed"} else normalized if normalized == "as" else "fixed",
    )


def _compute_glft(
    *,
    mid: float,
    inventory: float,
    sigma: float,
    gamma: float,
    intensity_a: float,
    intensity_k_rel: float,
    min_half_spread: float,
    max_half_spread: float,
    skew: float,
    half_spread_multiplier: float,
    tick_size: float,
) -> Quote:
    k_price = intensity_k_rel / mid if mid > 0 else max(intensity_k_rel, 1e-12)
    c1, c2 = glft_coefficients(gamma, intensity_a, k_price, lot=1.0)
    sigma = max(float(sigma), 0.0)
    psi = sigma * c2
    half_raw = (c1 + 0.5 * psi) * half_spread_multiplier
    half_spread = max(half_raw, min_half_spread)
    if max_half_spread > 0:
        half_spread = min(half_spread, max_half_spread)
        half_spread = max(half_spread, min_half_spread)
        psi = min(psi, max_half_spread)
    reservation = mid - inventory * psi + skew * half_spread
    bid = reservation - half_spread
    ask = reservation + half_spread
    bid, ask = _apply_tick(bid, ask, tick_size)
    return Quote(
        reservation=reservation,
        bid=bid,
        ask=ask,
        half_spread=half_spread,
        delta_bid=mid - bid,
        delta_ask=ask - mid,
        sigma=sigma,
        gamma=gamma,
        intensity_a=intensity_a,
        intensity_k=k_price,
        inventory=inventory,
        mode="glft",
    )
