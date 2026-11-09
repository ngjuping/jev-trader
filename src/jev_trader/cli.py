from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from jev_trader import __version__
from jev_trader.calibration.log import CalibrationLogger
from jev_trader.calibration.metrics import (
    apply_platt,
    brier_score,
    expected_calibration_error,
    fit_platt,
    load_pairs,
    log_loss,
    reliability_curve,
)
from jev_trader.config import Settings
from jev_trader.execution.paper import PaperVenue
from jev_trader.feeds.synthetic import SyntheticFeed
from jev_trader.judgment.client import JevClient
from jev_trader.judgment.fallback import HeuristicJudge
from jev_trader.loop.bitfinex_run import run_bitfinex
from jev_trader.loop.engine import TradingLoop


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-trader",
        description="24/7 market-making system around Jev System One decisions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    paper = sub.add_parser("paper", help="run the paper-trading loop")
    paper.add_argument("--blocks", type=int, default=2000)
    paper.add_argument("--seed", type=int, default=7)
    paper.add_argument("--block-ms", type=int, default=None)
    paper.add_argument("--realtime", action="store_true", help="pace blocks in real time")
    paper.add_argument("--log", type=Path, default=None)
    paper.add_argument("--no-log", action="store_true")

    live = sub.add_parser(
        "live",
        help="quote on Bitfinex with the dedicated sub-account keys",
    )
    _add_market_args(live, require_notional=True)
    live.add_argument(
        "--adopt",
        action="store_true",
        help="take over an existing position and orders on this symbol only",
    )

    shadow = sub.add_parser(
        "shadow",
        help="real Bitfinex book, paper fills, no orders",
    )
    _add_market_args(shadow, require_notional=False)

    calibrate = sub.add_parser("calibrate", help="calibration report from logged triples")
    calibrate.add_argument("--log", type=Path, default=None)
    calibrate.add_argument(
        "--source",
        choices=["all", "jev", "fallback"],
        default="all",
        help="filter decisions by judgment source",
    )

    sub.add_parser("models", help="list available TypeSafe models")
    return parser


def _add_market_args(parser: argparse.ArgumentParser, *, require_notional: bool) -> None:
    parser.add_argument("--symbol", default=None, help="default tBTCF0:USTF0 (or JEV_BFX__SYMBOL)")
    parser.add_argument(
        "--max-notional",
        type=float,
        required=require_notional,
        help="hard cap on position and order notional, in quote currency",
    )
    parser.add_argument(
        "--blocks",
        type=int,
        default=None,
        help="stop after N blocks; default runs until signal",
    )
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--fills-log", type=Path, default=None)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "paper":
        return asyncio.run(_run_paper(args))
    if args.command in {"live", "shadow"}:
        return asyncio.run(_run_bitfinex(args))
    if args.command == "calibrate":
        return _run_calibrate(args)
    if args.command == "models":
        return _run_models()
    return 1


async def _run_bitfinex(args: argparse.Namespace) -> int:
    settings = Settings()
    return await run_bitfinex(
        settings,
        mode=args.command,
        max_notional=args.max_notional,
        symbol=args.symbol,
        adopt=bool(getattr(args, "adopt", False)),
        blocks=args.blocks,
        log_path=args.log,
        fills_path=args.fills_log,
    )


async def _run_paper(args: argparse.Namespace) -> int:
    settings = Settings()
    block_ms = args.block_ms or settings.block_ms
    feed = SyntheticFeed(
        seed=args.seed,
        block_ms=block_ms,
        realtime=args.realtime,
        max_blocks=args.blocks,
    )
    venue = PaperVenue(
        starting_cash=settings.starting_cash,
        fee_bps=settings.fee_bps,
        tick_size=settings.pricing.tick_size,
        max_order_size=settings.risk.max_order_size,
    )
    log_path = args.log if args.log is not None else settings.log_path
    calibration = CalibrationLogger(log_path, enabled=not args.no_log)
    fallback = HeuristicJudge(max_position=settings.risk.max_position)

    async with AsyncExitStack() as stack:
        if settings.has_api_key:
            judge = await stack.enter_async_context(JevClient(settings))
            mode = f"jev ({settings.jev_model})"
        else:
            judge = fallback
            mode = "heuristic fallback (no TYPESAFE_API_KEY)"
        print(f"paper run: {args.blocks} blocks, block_ms={block_ms}, judgments={mode}")
        loop = TradingLoop(
            feed=feed,
            venue=venue,
            judge=judge,
            fallback=fallback,
            settings=settings,
            calibration=calibration,
        )
        stats = await loop.run()

    calibration.close()
    print(json.dumps(stats.summary(), indent=2))
    if not args.no_log:
        print(f"calibration log: {log_path}")
    return 0


def _run_calibrate(args: argparse.Namespace) -> int:
    settings = Settings()
    path = args.log if args.log is not None else settings.log_path
    if not path.exists():
        print(f"no calibration log at {path}; run `jev-trader paper` first")
        return 1
    source = None if args.source == "all" else args.source
    probabilities, outcomes = load_pairs(path, source=source)
    if len(probabilities) < 10:
        print(f"only {len(probabilities)} labeled pairs; need at least 10")
        return 1

    print(f"pairs: {len(probabilities)}  (source: {args.source})")
    print(f"brier:    {brier_score(probabilities, outcomes):.4f}")
    print(f"log loss: {log_loss(probabilities, outcomes):.4f}")
    print(f"ece:      {expected_calibration_error(probabilities, outcomes):.4f}")
    print("reliability curve:")
    print("  bin              n     predicted  empirical")
    for entry in reliability_curve(probabilities, outcomes):
        if entry.count == 0:
            continue
        print(
            f"  {entry.lower:.1f}-{entry.upper:.1f}"
            f"  {entry.count:6d}     {entry.mean_predicted:.3f}      {entry.empirical:.3f}"
        )

    a, b = fit_platt(probabilities, outcomes)
    calibrated = [apply_platt(p, a, b) for p in probabilities]
    print(
        f"platt: a={a:.4f} b={b:.4f}  brier "
        f"{brier_score(probabilities, outcomes):.4f} -> "
        f"{brier_score(calibrated, outcomes):.4f}"
    )
    return 0


def _run_models() -> int:
    from typesafe_sdk import TypeSafeClient

    settings = Settings()
    if not settings.has_api_key:
        print("TYPESAFE_API_KEY is not set")
        return 1
    with TypeSafeClient() as client:
        models = client.models.list()
    for model in models.models:
        print(f"{model.name:24s} {model.release_date}  {model.description}")
    return 0
