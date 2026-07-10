"""Daily portfolio simulator. Pure — no I/O.

Turns per-symbol trade signals (entry_date, exit_date) into a real account curve:
a capacity-capped book of `max_positions` equal-weight slots, marked daily off
each symbol's closes. This is what per-trade stats can't give — the actual
Sharpe, max drawdown, and capacity of a strategy once you can only hold so many
positions and split capital across them.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

Prices = dict[str, dict[str, float]]  # {symbol: {date: close}}


def simulate_portfolio(
    price: Prices,
    trades: list[dict[str, Any]],
    max_positions: int,
    cost_frac: float,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """Daily equity simulation of `trades` under a `max_positions` equal-weight
    book. Each slot gets 1/max_positions of capital; excess same-day signals are
    dropped (capacity). Positions are marked daily off closes; entry/exit cost is
    charged 1/max_positions per position. Returns {daily: [(date, ret)], stats}."""
    dates = sorted({d for s in price.values() for d in s})
    entries_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        entries_by_date[t["entry_date"]].append(t)

    open_pos: list[dict[str, Any]] = []
    daily: list[tuple[str, float]] = []
    admitted = 0
    pos_counts: list[int] = []

    for d in dates:
        # 1) mark open positions for the move into day d
        day_ret = 0.0
        for p in open_pos:
            c_prev = price[p["symbol"]].get(p["_prevd"])
            c_now = price[p["symbol"]].get(d)
            if c_prev and c_now:
                day_ret += (c_now / c_prev - 1.0) / max_positions
            p["_prevd"] = d
        cost_today = 0.0
        # 2) close positions exiting at end of day d
        keep = []
        for p in open_pos:
            if p["exit_date"] == d:
                cost_today += cost_frac / max_positions
            else:
                keep.append(p)
        open_pos = keep
        # 3) open new entries at end of day d (they earn from d+1), capacity-capped
        free = max_positions - len(open_pos)
        for t in entries_by_date.get(d, []):
            if free <= 0:
                break
            open_pos.append({"symbol": t["symbol"], "exit_date": t["exit_date"], "_prevd": d})
            cost_today += cost_frac / max_positions
            admitted += 1
            free -= 1
        daily.append((d, day_ret - cost_today))
        pos_counts.append(len(open_pos))

    return {
        "daily": daily,
        "stats": _stats(daily, admitted, pos_counts, max_positions, periods_per_year),
    }


def _stats(
    daily: list[tuple[str, float]],
    n_trades: int,
    pos_counts: list[int],
    max_positions: int,
    ppy: int,
) -> dict[str, Any]:
    rets = [r for _, r in daily]
    n = len(rets)
    if n == 0 or n_trades == 0:
        return {
            "n_trades": n_trades,
            "total_return": 0.0,
            "cagr": 0.0,
            "ann_vol": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "avg_positions": 0.0,
            "pct_invested": 0.0,
        }
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= 1.0 + r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / n
    std = var**0.5
    avg_pos = sum(pos_counts) / n
    return {
        "n_trades": n_trades,
        "total_return": equity - 1.0,
        "cagr": equity ** (ppy / n) - 1.0 if equity > 0 else -1.0,
        "ann_vol": std * (ppy**0.5),
        "sharpe": (mean / std) * (ppy**0.5) if std else 0.0,
        "max_drawdown": max_dd,
        "avg_positions": avg_pos,
        "pct_invested": avg_pos / max_positions,
    }
