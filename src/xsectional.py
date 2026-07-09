"""Cross-sectional daily reversal/momentum engine. Pure — no I/O.

Ranks a liquid daily universe by recent *formation* return, longs one tail and
shorts the other, holds `hold_days`, and measures the forward return spread.
Reversal = long losers / short winners; momentum = the inverse. Everything comes
from grouped daily bars (one call per date, cached), so long histories are cheap;
liquid names are borrowable, so the short leg is executable.

Rebalances are non-overlapping (step = hold_days) so each period is an
independent bet — no overlap autocorrelation to fool the stats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Panel = dict[str, dict[str, dict[str, float]]]


@dataclass(frozen=True)
class RebalanceResult:
    date: str
    long_ret: float  # mean forward return of the long basket
    short_ret: float  # mean forward return of the short basket
    n_long: int
    n_short: int


def build_panel(grouped_by_date: dict[str, list[dict[str, Any]]]) -> Panel:
    """{date: {symbol: {close, dollar_vol}}} from grouped rows (T/c/v)."""
    panel: Panel = {}
    for date_iso, rows in grouped_by_date.items():
        day: dict[str, dict[str, float]] = {}
        for r in rows:
            sym = r.get("T")
            close = r.get("c")
            if not sym or not close:
                continue
            vol = r.get("v", 0) or 0
            day[sym] = {"close": float(close), "dollar_vol": float(close) * float(vol)}
        panel[date_iso] = day
    return panel


def cross_sectional_backtest(
    panel: Panel,
    strategy: str,
    formation_days: int,
    hold_days: int,
    quantile: float,
    min_price: float,
    max_price: float,
    min_dollar_vol: float,
) -> list[RebalanceResult]:
    """Non-overlapping cross-sectional backtest. At each rebalance i (stepped by
    `hold_days`), rank eligible symbols by formation return over the prior
    `formation_days`, take the top/bottom `quantile` tails, assign long/short by
    `strategy`, and record each basket's forward return over `hold_days`."""
    dates = sorted(panel)
    out: list[RebalanceResult] = []
    i = formation_days
    while i + hold_days < len(dates):
        d0, d, d1 = dates[i - formation_days], dates[i], dates[i + hold_days]
        form: list[tuple[str, float, float]] = []  # (symbol, formation_ret, fwd_ret)
        for sym, cur in panel[d].items():
            past = panel[d0].get(sym)
            fut = panel[d1].get(sym)
            if not past or not fut:
                continue
            price = cur["close"]
            if not (min_price <= price <= max_price) or cur["dollar_vol"] < min_dollar_vol:
                continue
            if past["close"] <= 0 or price <= 0:
                continue
            form_ret = price / past["close"] - 1.0
            fwd_ret = fut["close"] / price - 1.0
            form.append((sym, form_ret, fwd_ret))

        n = len(form)
        k = max(1, int(n * quantile))
        if n < 2 * k:  # need disjoint long & short tails
            i += hold_days
            continue
        form.sort(key=lambda x: x[1])
        losers = form[:k]  # lowest formation return
        winners = form[-k:]  # highest formation return
        if strategy == "momentum":
            long_basket, short_basket = winners, losers
        else:  # "reversal"
            long_basket, short_basket = losers, winners
        long_ret = sum(x[2] for x in long_basket) / len(long_basket)
        short_ret = sum(x[2] for x in short_basket) / len(short_basket)
        out.append(
            RebalanceResult(date=d, long_ret=long_ret, short_ret=short_ret, n_long=k, n_short=k)
        )
        i += hold_days
    return out


def summarize_rebalances(results: list[RebalanceResult], cost_frac: float) -> dict[str, Any] | None:
    """Net long/short portfolio stats over rebalances (None if empty). Per-period
    pnl = long_ret − short_ret, minus `cost_frac` on *each* leg (round-trip both
    sides). Returns fractions (× 100 for %)."""
    if not results:
        return None
    pnls = [r.long_ret - r.short_ret - 2 * cost_frac for r in results]
    n = len(pnls)
    avg = sum(pnls) / n
    var = sum((x - avg) ** 2 for x in pnls) / n
    std = var**0.5
    return {
        "n": n,
        "avg": avg,
        "median": sorted(pnls)[n // 2],
        "win": sum(1 for x in pnls if x > 0) / n * 100,
        "std": std,
        "sharpe": (avg / std) if std else 0.0,  # per-rebalance; annualize downstream
    }
