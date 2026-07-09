"""Summary over accumulated short_setups rows. Pure — no I/O.

Answers the live question the accumulation exists for: does the *executable*
(shortable) subset of either short strategy behave differently from the
un-borrowable bulk? Groups the would-be outcomes by (strategy, shortable) with an
overall roll-up. The CLI reads the rows from SQLite and prints this.
"""

from __future__ import annotations

from typing import Any


def _stats(nets: list[float]) -> dict[str, float]:
    n = len(nets)
    return {
        "n": n,
        "avg": sum(nets) / n,
        "median": sorted(nets)[n // 2],
        "win": sum(1 for x in nets if x > 0) / n * 100,
    }


def summarize_short_setups(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Group would-be short outcomes by (strategy, shortable); None if empty.

    Each group carries n / avg / median / win; the top level carries the total
    count and the number of distinct sessions covered (dataset depth).
    """
    if not rows:
        return None
    buckets: dict[tuple[str, bool], list[float]] = {}
    sessions: set[str] = set()
    for r in rows:
        key = (str(r["strategy"]), bool(r["shortable"]))
        buckets.setdefault(key, []).append(float(r["net_return_pct"]))
        sessions.add(str(r["session_date"]))
    groups = [
        {"strategy": strat, "shortable": borrow, **_stats(nets)}
        for (strat, borrow), nets in sorted(buckets.items())
    ]
    return {"n": len(rows), "sessions": len(sessions), "groups": groups}
