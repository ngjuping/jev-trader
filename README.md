# jev-trader

A 24/7 market-making trading system built around [Jev](https://typesafe.ai), TypeSafe AI's
System One decision model. Jev returns calibrated, typed decisions in ~70–500 ms — inside a
300 ms block budget. This repo is the system that calls it: state engine, judgment battery,
policy engine, A-S pricing, hard risk vetoes, execution, and the calibration loop.

> **One principle carries the whole build:** Jev should not own the trading system.
> It should own selected judgments inside it. Code calculates the state. Jev interprets
> the state. Code applies policy. Execution places the order.

## Architecture

```
MARKET DATA
     |
FEATURE ENGINE                      deterministic, pure code
     |
STATE SNAPSHOT  (< 400 tokens)
     |
JEV  — one call, six atomic judgments
  regime | direction | toxic_flow | liquidity_stressed
  quote_environment | inventory_pressure
     |
PROBABILITY VECTOR (typed answers + confidence)
     |
POLICY ENGINE                       thresholds live in code
     |
  HARD RISK RULES  ------------->   absolute veto, never delegated
     |
EXECUTION (post-only quotes)
     |
CALIBRATION LOG (state, decision, outcome triples)
```

Everything computable stays in code (mid, microprice, spread, imbalance, realized vol,
inventory, drawdown, queue position). Everything that is a judgment goes to Jev.
Atomic questions, composed in code — change a coefficient, not a prompt.

## Quickstart

```bash
uv sync

# Run the paper loop in fallback mode (no API key needed)
uv run jev-trader paper --blocks 2000

# With live Jev judgments
export TYPESAFE_API_KEY="..."
uv run jev-trader paper --blocks 10000

# Realtime pacing (300 ms blocks) instead of accelerated simulation
uv run jev-trader paper --realtime

# Calibration report from the logged triples
uv run jev-trader calibrate

# List available models
uv run jev-trader models
```

Paper runs are deterministic for a given `--seed` and write JSONL calibration triples to
`data/calibration.jsonl` by default.

## Bitfinex (dedicated sub-account)

`shadow` and `live` trade one Bitfinex symbol, default `tBTCF0:USTF0`. They share the
same loop, risk checks, and calibration log. `shadow` reads the public book and
simulates fills. `live` sends post-only orders (flag 4096) on an authenticated
connection.

This process must use a **separate Bitfinex sub-account**. It refuses to start when
that account has a position, an open order, or a non-collateral balance it does not
own. `--adopt` takes over the configured symbol only; any other symbol is still a
hard stop. Do not put the main account's keys in `BFX_SUB_API_KEY` /
`BFX_SUB_API_SECRET`. Those variables are read from the environment and are never
logged or written by the program.

```bash
# Real book, paper fills, no orders. No Bitfinex key required.
uv run jev-trader shadow --symbol tBTCF0:USTF0 --max-notional 50

# Small live. --max-notional is mandatory. SIGTERM cancels quotes and flattens.
export BFX_SUB_API_KEY="..."
export BFX_SUB_API_SECRET="..."
export TYPESAFE_API_KEY="..."   # optional; heuristics run if this is unset
uv run jev-trader live --symbol tBTCF0:USTF0 --max-notional 50
```

Recommended limits for a first live run on `tBTCF0:USTF0` with about $200 on the
sub-account (the BTC perp minimum size is about 0.0004 BTC):

```bash
export JEV_BFX__LEVERAGE=1
export JEV_BLOCK_MS=1000
export JEV_RISK__MAX_POSITION=0.0004
export JEV_RISK__MAX_ORDER_SIZE=0.0004
export JEV_RISK__MAX_DAILY_LOSS=10
export JEV_RISK__MAX_DRAWDOWN=0.05
export JEV_RISK__MAX_STALE_DATA_AGE_S=20
export JEV_RISK__MAX_API_ERRORS=5
export JEV_RISK__MAX_INVENTORY_AGE_S=300
uv run jev-trader live --max-notional 50
```

`--max-notional 50` caps position and order notional in the quote currency. A hard
limit (daily loss, drawdown, notional, repeated API errors), a stale book, or
SIGTERM cancels quotes and, on live, flattens with a reduce-only market order.
The bot does not quote while the book is unsynced.

Fills go to `data/fills.jsonl` with markouts at 1s, 5s, and 30s. Calibration
triples still go to `data/calibration.jsonl`. Maker and taker fees come from
`auth/r/summary` when the sub-account keys are present, otherwise from
`JEV_BFX__MAKER_FEE_RATE` / `JEV_BFX__TAKER_FEE_RATE` or a conservative default.
Fees are included in PnL.

### systemd

`deploy/jev-trader.service` is a template. Copy `deploy/env.example` to
`/etc/jev-trader/env` (mode `0600`), fill in the sub-account keys there, and point
the unit at that file. Nothing in this repo contains a key.

```bash
sudo install -m 0600 deploy/env.example /etc/jev-trader/env
sudo cp deploy/jev-trader.service /etc/systemd/system/jev-trader.service
sudo systemctl daemon-reload
sudo systemctl enable --now jev-trader
```

### Limitations

- There is no Bitfinex testnet. `shadow` is the paper path. This change does not
  start a live session and does not read real keys.
- Routine requotes cancel and replace by order id. A kill or SIGTERM cancels
  resting orders on the sub-account, then sends the flatten. `cancel_all` does
  not pull an in-flight reduce-only order.
- The public book heartbeat is 15 seconds. The default 2 second stale limit will
  hold quotes on a quiet book; use `JEV_RISK__MAX_STALE_DATA_AGE_S=20` for live.
- Quoting every block can hit Bitfinex order rate limits. `JEV_BLOCK_MS=1000` is
  the safer live cadence. Repeated API failures trip the kill switch.
- Shadow fills are trade-through prints, not a queue-position model.
- Derivative funding is not a separate PnL line. Spot flatten is an exchange
  market order; the reduce-only flag applies to perps.
- A checksum mismatch resubscribes. Price precision follows Bitfinex's 5-significant-figure
  rule. The minimum order size comes from the conf endpoint.

## Layout

| Module | Responsibility |
| --- | --- |
| `state/` | L2 book math, feature engine, dense snapshot builder |
| `judgment/` | Six-question battery, async Jev client, deterministic fallback judge |
| `policy/` | Action composition (thresholds in code), fractional Kelly sizing |
| `pricing/` | Avellaneda-Stoikov reservation price, half spread, inventory skew |
| `risk/` | Hard-coded limits, absolute veto, kill switch |
| `execution/` | `VenueAdapter` protocol, paper venue with post-only lifecycle + fills |
| `loop/` | Nine-stage loop, block-deadline rule, fallback ladder |
| `calibration/` | JSONL triple logging, Brier / log loss / ECE / reliability / Platt |

## Fallback ladder

The system is autonomous only if it degrades in order:

```
healthy + high confidence  -> normal operation
healthy + low confidence   -> reduce size
late past block deadline   -> hold, never quote stale state
Jev unavailable            -> deterministic fallback judgments only
hard limit breached        -> kill switch, flatten, stop
```

## Configuration

All config is env-driven (`TYPESAFE_API_KEY`, `JEV_*`). See `.env.example` for the full set.
Risk limits are hard-coded vetoes and are never delegated to the model.

## Calibration

Every block logs the triple `(state, decision, outcome)`. `jev-trader calibrate` reports
Brier score, log loss, expected calibration error, and a reliability curve, and fits Platt
scaling so policy thresholds can be recalibrated on your venue's data — RLCD calibrates
against TypeSafe's distribution, not yours.

## Roadmap

See [PLAN.md](PLAN.md). Current status: Phase 0 (foundation + paper loop) complete.

## Disclaimer

This software is for research and educational purposes. It is not financial advice.
Trading involves substantial risk of loss. Run in paper mode and testnet before risking
capital, and never disable the risk engine.
