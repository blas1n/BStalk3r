"""Offline replay / backtest over accumulated screened runners + outcomes.

Apply an alternate parameter set to the stored runners, see which would have
been entered, and score that entry set with the recorded forward outcomes.
Reuses the *live* `scanner.passes_filters`, so replay can never drift from the
real entry logic — the whole reason scanner/strategy/risk were kept pure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any

from src.models import MarketSnapshot
from src.scanner import ScanFilters, passes_filters


def round_trip_cost(
    price: float, base_pct: float, cheap_price: float, cheap_extra_pct: float
) -> float:
    """Estimated round-trip transaction cost (%) for one trade.

    Low-priced names trade with much wider relative spreads/slippage, so add a
    surcharge below `cheap_price`. This is a research assumption (grouped EOD
    data has no real quote), not a measured spread — tune to taste.
    """
    return base_pct + (cheap_extra_pct if price < cheap_price else 0.0)


@dataclass(frozen=True)
class ReplayResult:
    name: str
    n_entered: int  # passed the filter
    n_scored: int  # passed the filter AND has a forward outcome
    avg_return: float | None
    median_return: float | None
    win_rate: float | None
    avg_max_gain: float | None
    avg_max_drawdown: float | None
    symbols: list[str] = field(default_factory=list)


def _snapshot(row: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=row["symbol"],
        last_price=row["last_price"],
        day_change_pct=row["day_change_pct"],
        rvol=row["rvol"],
        volume_acceleration=row.get("volume_acceleration", 1.0),
        spread_pct=row.get("spread_pct", 0.0),
    )


def simulate(
    name: str,
    rows: list[dict[str, Any]],
    filters: ScanFilters,
    cost_fn: Callable[[dict[str, Any]], float] | None = None,
) -> ReplayResult:
    """Entry set under `filters`, scored by the rows' forward outcomes.

    Each row carries screened fields plus `ret` / `max_gain` / `max_drawdown`
    for one horizon (None when the outcome isn't computed yet). Unscored entries
    count toward `n_entered` but are excluded from the metrics.

    `cost_fn(row) -> pct` is the round-trip transaction cost subtracted from each
    trade's return / max-gain / max-drawdown — so metrics are net of costs. With
    no cost_fn the result is gross (back-compatible).
    """
    cost = cost_fn or (lambda r: 0.0)
    entered = [r for r in rows if passes_filters(_snapshot(r), filters)]
    scored = [r for r in entered if r.get("ret") is not None]
    rets = [r["ret"] - cost(r) for r in scored]

    return ReplayResult(
        name=name,
        n_entered=len(entered),
        n_scored=len(scored),
        avg_return=mean(rets) if rets else None,
        median_return=median(rets) if rets else None,
        win_rate=(sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
        avg_max_gain=(mean([r["max_gain"] - cost(r) for r in scored]) if scored else None),
        avg_max_drawdown=(mean([r["max_drawdown"] - cost(r) for r in scored]) if scored else None),
        symbols=[r["symbol"] for r in entered],
    )
