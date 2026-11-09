# Jev Trader — Build Plan

A 24/7 market-making system built around **Jev** (TypeSafe AI's System One decision
model). The design principle that carries the whole build:

> **Jev should not own the trading system. It should own selected judgments inside it.**
>
> Code calculates the state. Jev interprets the state. Code applies policy.
> Execution places the order. The hard risk layer always wins.

The loop is nine stages, each a tested module with its own failure modes:

```
Block event (WebSocket newHeads + polling backstop)
  -> read L2 book (best bid/ask/depth)
  -> compute state snapshot (deterministic, dense, < 400 tokens)
  -> fire Jev battery (six judgments, one call, one latency)
  -> policy engine composes action (thresholds live in code)
  -> A-S pricing computes reservation + spread
  -> risk engine checks hard limits (absolute veto)
  -> cancel old quotes, post new post-only orders
  -> log snapshot + decision + outcome (calibration triples)
  -> next block
```

## Status legend

- [x] done in this repo
- [ ] planned

---

## Phase 0 — Foundation (current)

Scaffold the full loop against a paper venue so every interface is exercised
before real money or a real venue touches it.

- [x] Repo, `uv` toolchain, ruff + pytest + CI
- [x] Config via env (`Settings`, nested risk/policy/pricing configs)
- [x] Core types (`JudgmentSet`, `Action`, `Order`, `Fill`, `PnL`)
- [x] State engine: `OrderBook` math (mid, microprice, spread bps, depth, imbalance)
- [x] Feature engine: returns 1m/5m/30m, realized vol, vol ratio, flow intensity
- [x] State snapshot builder (dense numeric dict for Jev)
- [x] Judgment battery: 6 atomic questions in one call (2 Choice, 2 Noul, 2 Score)
- [x] `JevClient` (async SDK wrapper, pinned model, per-call latency + request id)
- [x] `HeuristicJudge` deterministic fallback (used when Jev is unavailable)
- [x] Policy engine with code-owned thresholds + fractional Kelly sizing
- [x] Avellaneda-Stoikov pricing (reservation price + optimal half spread + skew)
- [x] Risk engine: hard-coded vetoes, precedence `KILL > HOLD > REDUCE_ONLY > OK`
- [x] Paper venue: post-only order lifecycle, trade-through fills, inventory + PnL
- [x] Synthetic block feed (seeded, deterministic, realtime or accelerated)
- [x] 24/7 loop with block-deadline rule and fallback ladder
- [x] Calibration logging (decision + outcome JSONL triples)
- [x] Calibration metrics: Brier, log loss, ECE, reliability curve, Platt scaling
- [x] CLI: `paper`, `calibrate`, `models`
- [x] Test suite for every deterministic module

**Acceptance:** `uv run pytest` green, `uv run jev-trader paper --blocks 2000` runs
end to end without a TypeSafe key (fallback mode) and writes calibration triples.

## Phase 1 — Live Jev hardening

- [ ] Apply for a TypeSafe key, run `paper` in live-judgment mode (heuristics off)
- [ ] Measure real decision latency distribution per block; tune `decision_timeout_s`
- [ ] Log and assert the returned `model` matches the pinned version (silent-upgrade guard)
- [ ] Token accounting: cost per 1M decisions from `usage`, per the $10–25/month claim
- [ ] Verify parallel-battery economics: 6 questions, single-question latency
- [ ] Retry/timeout policy review: never retry past the block deadline

**Acceptance:** 10k blocks with live Jev, zero stale-state quotes, latency p99
inside the block budget, cost per block measured.

## Phase 2 — Realistic simulation

- [ ] Queue-position fill model (fills require trade-through, not touch)
- [ ] Latency simulation: order ack + cancel/replace round trip
- [ ] Gas / fee model for cancel-replace every block (the "gas honesty check")
- [ ] Historical L2 replay feed (recorded book + trades)
- [ ] Adverse-selection metrics: markout at 1/5/10/30 blocks after each fill
- [ ] Spread/time-in-book tuning: widen, rest longer, or find directional edge

**Acceptance:** simulated cancel-replace cost model reproduces the structural-loss
result from the article's critique; a configuration with positive net edge exists
in simulation or the strategy is rejected.

## Phase 3 — Calibration & backtest harness

- [ ] Four baselines on identical data: hand rules, frontier LLM layer, Jev, Jev + confidence gating
- [ ] Metrics: Sharpe, Sortino, max drawdown, hit rate, slippage, adverse selection,
      cost per 1M decisions, coverage
- [ ] Reliability curves from logged triples; Brier / log loss / ECE
- [ ] Platt scaling in the policy layer when the reliability curve bends
- [ ] Threshold sweep: one threshold per action, scaled to cost of being wrong
- [ ] Answer the real research question: does abstaining when uncertain improve the book?

**Acceptance:** published backtest report in `docs/` with the four-way comparison.

## Phase 4a — Bitfinex dedicated sub-account

The owner chose small live on a separate Bitfinex sub-account (2026-11-09).
The main account, which runs an unrelated directional trader, is not a target:
live mode requires `BFX_SUB_API_KEY` / `BFX_SUB_API_SECRET` and refuses to start
when the account holds exposure this process does not own.

- [x] `BitfinexVenue` (`VenueAdapter`): P0 book len 25, trades, post-only flag 4096, cancel/replace, cancel-all, fills, REST reconcile, conf precision
- [x] `BitfinexFeed` block clock (`JEV_BLOCK_MS`) into the existing state engine; no quotes while the book is unsynced
- [x] `live` (mandatory `--max-notional`) and `shadow` (real book, paper fills, no orders)
- [x] Kill switch: hard limits, repeated API errors, blind book, SIGTERM → cancel + reduce-only flatten
- [x] Account fees in PnL, calibration triples, fills ledger with 1/5/30s markouts
- [x] systemd unit template; secrets only via env
- [ ] Operator runs shadow, then small live, on the sub-account (not done from this repo)

## Phase 4 — Live venue adapter

- [ ] Venue adapter interface (`VenueAdapter` protocol) hardened for async IO
- [ ] Target selection: Monad/Kuru MON-USDC, Hyperliquid, or Solana DEX
- [ ] WebSocket `newHeads` block events + polling backstop
- [ ] Post-only limit order submission, nonce management, tx confirmation tracking
- [ ] Balance/inventory reconciliation against on-chain state
- [ ] Kill switch: cancel-all + flatten path tested on testnet

**Acceptance:** 24h testnet run, no orphan orders, no stale quotes, kill switch
demonstrated live.

## Phase 5 — Production 24/7

- [ ] Alerting (latency, error rate, drawdown, Jev unavailability)
- [ ] Dashboard: equity, inventory, decision latency, action mix, calibration drift
- [ ] Secret management, process supervision, restart-with-state recovery
- [ ] Canary deployment: tiny size, real venue, watched
- [ ] Runbook: what to do when the fallback ladder fires

**Acceptance:** one week unattended with the fallback ladder exercised at least
once and handled correctly.

## Phase 6 — Research

- [ ] Directional edge from the battery (does `direction` add edge after costs?)
- [ ] Multi-venue quoting and cross-venue divergence feature (`CROSS` block)
- [ ] Fractional Kelly sizing gated on verified calibration
- [ ] Model upgrade procedure: recalibrate thresholds on version change
- [ ] Prediction markets (200–500 bps spreads) as a second target venue

---

## Non-goals (the honest contract)

Jev cannot: generate text or design the strategy, chain dependent judgments in one
call, compete in the microsecond lane, replace market data infra, numerical
computation, exchange connectivity, or hard risk controls, or turn a losing
strategy into a winning one. Jev makes decisions cheap and fast. Edge is still
our job.
