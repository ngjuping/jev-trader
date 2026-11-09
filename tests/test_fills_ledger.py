from __future__ import annotations

import json

from jev_trader.calibration.fills import FillsLedger
from jev_trader.types import Fill


def test_markouts_at_1_5_and_30_seconds(tmp_path):
    path = tmp_path / "fills.jsonl"
    ledger = FillsLedger(path)
    ledger.record_fill(
        ts=1_000.0,
        mid=100.0,
        fill=Fill(side="buy", price=100.0, size=0.01, fee=0.002, ts=1_000.0, trade_id="t1"),
    )
    ledger.observe(1_000.5, 100.1)
    ledger.observe(1_001.0, 100.2)
    ledger.observe(1_005.0, 99.5)
    ledger.observe(1_030.0, 101.0)
    ledger.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    fills = [row for row in rows if row["kind"] == "fill"]
    markouts = [row for row in rows if row["kind"] == "markout"]
    assert len(fills) == 1
    assert fills[0]["fee"] == 0.002
    horizons = [row["horizon_s"] for row in markouts]
    assert horizons == [1.0, 5.0, 30.0]
    assert markouts[0]["markout_bps"] == 20.0
    assert markouts[1]["markout_bps"] < 0
    assert markouts[2]["markout_bps"] == 100.0
