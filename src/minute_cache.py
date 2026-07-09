"""Persistent minute-bar cache — a drop-in `MinuteBarsProvider` wrapper.

Polygon free tier is 5 req/min, so iterative strategy search re-pays that latency
on every run. This wraps any `MinuteBarsProvider` with a SQLite store so each
(symbol, date) hits the API at most once ever — subsequent backtests read from
disk and run instantly over long windows. Empty results are cached too (a
historical date with no data won't gain any), so known-empty days aren't
re-fetched.

The cache is intentionally a *superset* of one session's provider: build it up
over time (the daily job can extend it), then run unlimited fast backtests.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bar_cache (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    bars_json TEXT NOT NULL,
    n_bars INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def _encode(bars: list[dict[str, Any]]) -> str:
    return json.dumps([{**b, "ts": b["ts"].isoformat()} for b in bars], separators=(",", ":"))


def _decode(blob: str) -> list[dict[str, Any]]:
    rows = json.loads(blob)
    for b in rows:
        b["ts"] = datetime.fromisoformat(b["ts"])
    return rows


class CachedMinuteBars:
    """MinuteBarsProvider that reads through a SQLite cache to `inner`."""

    def __init__(self, inner: Any, db_path: str):
        self._inner = inner
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def fetch(self, symbol: str, date: str) -> list[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT bars_json FROM minute_bar_cache WHERE symbol=? AND date=?", (symbol, date)
        ).fetchone()
        if row is not None:
            return _decode(row["bars_json"])
        bars = self._inner.fetch(symbol, date)
        self._conn.execute(
            "INSERT OR REPLACE INTO minute_bar_cache "
            "(symbol, date, bars_json, n_bars, fetched_at) VALUES (?,?,?,?,?)",
            (symbol, date, _encode(bars), len(bars), datetime.now().astimezone().isoformat()),
        )
        self._conn.commit()
        return bars

    def stats(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS days, COALESCE(SUM(n_bars),0) AS bars FROM minute_bar_cache"
        ).fetchone()
        return {"symbol_days": int(row["days"]), "total_bars": int(row["bars"])}
