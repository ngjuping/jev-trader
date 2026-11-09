from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskConfig(BaseModel):
    """Hard risk limits. Absolute veto, never delegated to the model."""

    model_config = ConfigDict(extra="ignore")

    max_position: float = 100.0
    max_order_size: float = 25.0
    max_daily_loss: float = 250.0
    max_drawdown: float = 0.05
    max_inventory_age_s: float = 900.0
    max_stale_data_age_s: float = 2.0
    max_decision_latency_ms: float = 250.0
    max_api_errors: int = 5
    kill_position_multiplier: float = 1.5
    # Quote-currency cap. 0 disables it. `live` sets this from --max-notional.
    max_notional: float = 0.0


class PolicyConfig(BaseModel):
    """Policy thresholds. They live in code, not in the model."""

    model_config = ConfigDict(extra="ignore")

    toxic_pull: float = 0.60
    liquidity_widen: float = 0.70
    quote_min_score: float = 2.0
    quote_min_confidence: float = 0.80
    quote_wide_min_score: float = 1.0
    low_confidence_scale: float = 0.5
    wide_spread_multiplier: float = 2.0
    kelly_cap: float = 0.25


class BitfinexConfig(BaseModel):
    """Bitfinex connectivity. Secrets are NOT here; they are `BFX_SUB_*` env vars."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = "tBTCF0:USTF0"
    # Empty derives spot vs perp from the symbol (`F0` => perp).
    market: Literal["", "spot", "perp"] = ""
    public_ws_url: str = "wss://api-pub.bitfinex.com/ws/2"
    private_ws_url: str = "wss://api.bitfinex.com/ws/2"
    rest_url: str = "https://api.bitfinex.com"
    public_rest_url: str = "https://api-pub.bitfinex.com"
    # Decimal fee rates. None reads the account summary (live) or the conservative default.
    maker_fee_rate: float | None = None
    taker_fee_rate: float | None = None
    leverage: int = Field(default=1, ge=1, le=100)
    reconcile_interval_s: float = 30.0
    order_ack_timeout_s: float = 5.0
    max_blind_s: float = 30.0
    min_order_size: float | None = None


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gamma: float = 0.5
    kappa: float = 100.0
    horizon_s: float = 60.0
    min_half_spread_bps: float = 1.0
    tick_size: float = 0.01
    # --- anti-adverse-selection (local microstructure, computed every block) ---
    # 1. center the reservation price on the top-of-book microprice instead of mid.
    use_microprice: bool = True
    # 2. OFI (CKS) over a rolling window + OBI blended into s in [-1, 1];
    #    the center shifts by alpha_bps * s toward the predicted direction.
    micro_alpha_bps: float = 1.5
    ofi_window_s: float = 3.0
    obi_levels: int = 3
    signal_w_ofi: float = 0.5
    signal_w_obi: float = 0.5
    # 3. one-sided pull: |s| > threshold drops the side that would be picked off.
    one_sided_threshold: float = 0.6
    # 4. toxicity pause: big aggressive trade or sharp mid jump between blocks pulls both sides.
    toxic_pause_s: float = 1.5
    toxic_jump_bps: float = 3.0
    toxic_trade_quantile: float = 0.95
    toxic_trade_median_mult: float = 3.0
    toxic_trade_window_s: float = 300.0
    toxic_trade_min_samples: int = 50
    # never quote through the touch (avoids post-only rejects after skew/shift).
    clamp_to_touch: bool = True
    micro_log_every_blocks: int = 30


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JEV_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    typesafe_api_key: SecretStr | None = Field(
        default=None, validation_alias="TYPESAFE_API_KEY"
    )
    # Dedicated sub-account only. Never the main account that runs the directional trader.
    bfx_sub_api_key: SecretStr | None = Field(default=None, validation_alias="BFX_SUB_API_KEY")
    bfx_sub_api_secret: SecretStr | None = Field(
        default=None, validation_alias="BFX_SUB_API_SECRET"
    )
    jev_model: str = "jev-latest"
    block_ms: int = 300
    decision_timeout_s: float = 0.25
    calibration_horizon_blocks: int = 10
    log_path: Path = Path("data/calibration.jsonl")
    fills_path: Path = Path("data/fills.jsonl")
    starting_cash: float = 10_000.0
    fee_bps: float = 1.0

    risk: RiskConfig = Field(default_factory=RiskConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    bfx: BitfinexConfig = Field(default_factory=BitfinexConfig)

    @property
    def has_api_key(self) -> bool:
        return self.typesafe_api_key is not None and bool(
            self.typesafe_api_key.get_secret_value()
        )

    def subaccount_keys(self) -> tuple[str, str] | None:
        key = _secret_value(self.bfx_sub_api_key)
        secret = _secret_value(self.bfx_sub_api_secret)
        if not key or not secret:
            return None
        return key, secret


def _secret_value(value: SecretStr | None) -> str:
    if value is None:
        return ""
    return value.get_secret_value().strip()
