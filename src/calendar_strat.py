"""Calendar/seasonality engine — Turn-of-Month. Pure — no I/O.

Structural driver (month-end institutional rebalancing, not arbitrage): long an
equal-weight liquid basket only during the TOM window (last `days_before` trading
days of a month + first `days_after` of the next), flat otherwise. Research: TOM
is the only persistently significant calendar effect.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def classify_tom(dates: list[str], days_before: int, days_after: int) -> set[str]:
    """Set of dates in the turn-of-month window: the last `days_before` trading
    days of each month plus the first `days_after` trading days of each month
    (which together straddle every month boundary)."""
    by_month: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in sorted(dates):
        y, m, _ = d.split("-")
        by_month[(y, m)].append(d)
    window: set[str] = set()
    for month_dates in by_month.values():
        if days_after > 0:
            window.update(month_dates[:days_after])
        if days_before > 0:
            window.update(month_dates[-days_before:])
    return window


def equal_weight_returns(
    panel: dict[str, dict[str, dict[str, float]]],
    min_price: float,
    max_price: float,
    min_dollar_vol: float,
) -> list[tuple[str, float]]:
    """Daily equal-weight return of the eligible (liquid, in-band) universe."""
    dates = sorted(panel)
    out: list[tuple[str, float]] = []
    for i in range(1, len(dates)):
        prev, cur = panel[dates[i - 1]], panel[dates[i]]
        rets = []
        for sym, rec in cur.items():
            p = prev.get(sym)
            if not p or p["close"] <= 0:
                continue
            price = rec["close"]
            if not (min_price <= price <= max_price) or rec["dollar_vol"] < min_dollar_vol:
                continue
            rets.append(price / p["close"] - 1.0)
        if rets:
            out.append((dates[i], sum(rets) / len(rets)))
    return out


def calendar_trades(
    ew_returns: list[tuple[str, float]], window: set[str], cost_frac: float
) -> list[dict[str, Any]]:
    """Group contiguous window days into runs; each run is one long trade earning
    the compounded EW return over its days, net of a round-trip cost."""
    trades: list[dict[str, Any]] = []
    run: list[tuple[str, float]] = []

    def _close(r: list[tuple[str, float]]) -> dict[str, Any]:
        comp = 1.0
        for _, ret in r:
            comp *= 1.0 + ret
        return {
            "entry_date": r[0][0],
            "exit_date": r[-1][0],
            "ret": (comp - 1.0) - 2 * cost_frac,
            "days": len(r),
        }

    for date, ret in ew_returns:
        if date in window:
            run.append((date, ret))
        elif run:
            trades.append(_close(run))
            run = []
    if run:
        trades.append(_close(run))
    return trades


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Net trade-return stats (None if empty)."""
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    n = len(rets)
    avg = sum(rets) / n
    var = sum((x - avg) ** 2 for x in rets) / n
    std = var**0.5
    return {
        "n": n,
        "avg": avg,
        "median": sorted(rets)[n // 2],
        "win": sum(1 for x in rets if x > 0) / n * 100,
        "std": std,
        "sharpe": (avg / std) if std else 0.0,
    }
