"""Daily portfolio simulator. Pure — no I/O.

Turns per-symbol trade signals (entry_date, exit_date) into a real account curve:
a capacity-capped book of `max_positions` equal-weight slots, marked daily off
each symbol's closes. This is what per-trade stats can't give — the actual
Sharpe, max drawdown, and capacity of a strategy once you can only hold so many
positions and split capital across them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

Prices = dict[str, dict[str, float]]  # {symbol: {date: close}}


def mr_rank_key(t: dict[str, Any]) -> tuple[float, int, float, str]:
    """Same-day admission order of the live selector (`mr_decisions`, R5): entry
    RSI ascending, then 20-day average dollar volume descending (unknown after
    known), then symbol. A trade without `entry_rsi` raises — it must not rank."""
    v = t.get("rank_dollar_vol")
    return (float(t["entry_rsi"]), 0 if v is not None else 1, -(v or 0.0), t["symbol"])


def simulate_portfolio(
    price: Prices,
    trades: list[dict[str, Any]],
    max_positions: int,
    cost_frac: float,
    periods_per_year: int = 252,
    rank_key: str | Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Daily equity simulation of `trades` under a `max_positions` equal-weight
    book. Each slot gets 1/max_positions of capital; excess same-day signals are
    dropped (capacity). Positions are marked daily off closes; entry/exit cost is
    charged 1/max_positions per position. Returns {daily: [(date, ret)], stats}.

    `rank_key`: when set, same-day signals competing for scarce slots are admitted
    by ascending trade[rank_key] (e.g. 'entry_rsi' → most-oversold first), or by
    ascending rank_key(trade) when it is callable (e.g. `mr_rank_key`), instead
    of first-come. `taken` lists the admitted (entry_date, symbol) in order."""
    dates = sorted({d for s in price.values() for d in s})
    entries_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        entries_by_date[t["entry_date"]].append(t)
    if callable(rank_key):
        for lst in entries_by_date.values():
            lst.sort(key=rank_key)
    elif rank_key is not None:
        key = rank_key
        for lst in entries_by_date.values():
            lst.sort(key=lambda t: t.get(key, 0.0))

    open_pos: list[dict[str, Any]] = []
    daily: list[tuple[str, float]] = []
    admitted = 0
    taken: list[tuple[str, str]] = []  # (entry_date, symbol) admitted, in order
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
            taken.append((d, t["symbol"]))
            free -= 1
        daily.append((d, day_ret - cost_today))
        pos_counts.append(len(open_pos))

    return {
        "daily": daily,
        "taken": taken,
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
