from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from jev_trader.types import Fill

MARKOUT_HORIZONS_S = (1.0, 5.0, 30.0)


@dataclass
class _PendingMarkout:
    fill_id: str
    side: str
    price: float
    due: float
    horizon_s: float


class FillsLedger:
    """JSONL fills plus markouts at 1s, 5s, and 30s.

    Markout is signed in basis points: positive means the mid moved in the
    fill's favor (a buy followed by a higher mid). The clock starts when the
    fill is recorded.
    """

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self._path = path
        self._enabled = enabled
        self._file: TextIO | None = None
        self._pending: list[_PendingMarkout] = []
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    def record_fill(self, *, ts: float, mid: float, fill: Fill) -> str:
        fill_id = fill.trade_id or uuid.uuid4().hex[:12]
        anchor = fill.ts if fill.ts is not None else ts
        if self._file is not None:
            self._write(
                {
                    "kind": "fill",
                    "id": fill_id,
                    "ts": round(anchor, 3),
                    "mid": mid,
                    "side": fill.side,
                    "price": fill.price,
                    "size": fill.size,
                    "fee": fill.fee,
                    "order_id": fill.order_id,
                }
            )
        for horizon in MARKOUT_HORIZONS_S:
            self._pending.append(
                _PendingMarkout(
                    fill_id=fill_id,
                    side=fill.side,
                    price=fill.price,
                    due=anchor + horizon,
                    horizon_s=horizon,
                )
            )
        return fill_id

    def observe(self, ts: float, mid: float) -> None:
        if mid <= 0:
            return
        still: list[_PendingMarkout] = []
        for pending in self._pending:
            if ts + 1e-9 < pending.due:
                still.append(pending)
                continue
            sign = 1.0 if pending.side == "buy" else -1.0
            markout_bps = 0.0
            if pending.price > 0:
                markout_bps = sign * (mid - pending.price) / pending.price * 10_000.0
            if self._file is not None:
                self._write(
                    {
                        "kind": "markout",
                        "id": pending.fill_id,
                        "horizon_s": pending.horizon_s,
                        "ts": round(ts, 3),
                        "mid": mid,
                        "markout_bps": round(markout_bps, 4),
                    }
                )
        self._pending = still

    def _write(self, record: dict[str, Any]) -> None:
        assert self._file is not None
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> FillsLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
