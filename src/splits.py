"""Split adjustment for grouped-cache closes. Pure — no I/O.

The grouped daily cache stores each session as it was fetched, so a split shows up
as a one-day jump by the split ratio (e.g. a -90% "crash" on a 10:1 split).
Backtests on raw closes book that jump as P&L. This is the heuristic the research
simulator uses (`research/tiebreak/core.py` `split_adjust`), ported so `src/`
backtests and the research agree (a test pins the ratios to core.py):

  a close/previous-close ratio with a move above 40% (r > 1.4 or r < 0.6) that is
  within 3% of a standard split ratio is treated as a split, and every earlier
  close of that symbol is multiplied by the ratio (back-adjusted into post-split
  units). Consecutive splits compound.

Limits: a real move that lands within 3% of a ratio is adjusted away, and a split
coinciding with a large same-day move (ratio off the list) is not caught. Dollar
volume stays raw (the price actually traded), as in the research.
"""

from __future__ import annotations

from typing import Any

SPLIT_K = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 35, 40, 50, 60, 75, 80, 100, 150, 200, 250]
SPLIT_R = sorted(
    {k for k in SPLIT_K} | {1 / k for k in SPLIT_K} | {1.5, 2 / 3, 2.5, 0.4, 4 / 3, 0.75}
)
MOVE_UP, MOVE_DOWN, RATIO_TOL = 1.4, 0.6, 0.03


def split_adjust_closes(
    closes: list[float],
) -> tuple[list[float], list[tuple[int, float, float]]]:
    """Back-adjusted closes and detected events as (index, observed ratio, split
    ratio); `index` is the first post-split bar."""
    adj = list(closes)
    events: list[tuple[int, float, float]] = []
    for t in range(1, len(closes)):
        prev, cur = closes[t - 1], closes[t]
        if prev <= 0 or cur <= 0:
            continue
        x = cur / prev
        if not (x > MOVE_UP or x < MOVE_DOWN):
            continue
        best = min(SPLIT_R, key=lambda s: abs(x / s - 1))
        if abs(x / best - 1) < RATIO_TOL:
            events.append((t, x, best))
            for i in range(t):
                adj[i] *= best
    return adj, events


def split_adjust_panel(
    panel: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Copy of a {date: {symbol: {close, dollar_vol, ...}}} panel with each
    symbol's closes split-adjusted over the dates it trades; other fields raw."""
    dates = sorted(panel)
    by_sym: dict[str, list[str]] = {}
    for d in dates:
        for sym in panel[d]:
            by_sym.setdefault(sym, []).append(d)
    out: dict[str, dict[str, dict[str, Any]]] = {
        d: {sym: dict(rec) for sym, rec in panel[d].items()} for d in dates
    }
    for sym, ds in by_sym.items():
        adj, events = split_adjust_closes([panel[d][sym]["close"] for d in ds])
        if events:
            for d, c in zip(ds, adj, strict=True):
                out[d][sym]["close"] = c
    return out
