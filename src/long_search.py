"""Long-strategy search core. Pure — no I/O.

Runs a grid of (entry shape × exit params) over the honest crosser universe with
a chronological train/test(/holdout) split. The point is discipline: optimise on
train, then *show* how each combo does out-of-sample on test, so a train winner
that's really an overfit mirage is exposed rather than shipped. The CLI fetches
the crosser bars (cached) and touches holdout once for the final pick.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.intraday import (
    aggregate,
    reconstruct_entry,
    reconstruct_gap_and_go_entry,
    reconstruct_orb_entry,
    reconstruct_pullback_entry,
    simulate_trade,
)
from src.strategy import ExitParams


@dataclass(frozen=True)
class TradeInput:
    """One crosser on one session: the minute bars + its prior close."""

    symbol: str
    date: str
    prev_close: float
    bars: list[dict[str, Any]]


def make_grid(
    shapes: list[str],
    entry_min_change: list[float],
    stop: list[float],
    take_profit: list[float],
    trailing: list[float],
    max_hold: list[float],
) -> list[dict[str, Any]]:
    """Cartesian product of the parameter axes into combo dicts."""
    keys = ["shape", "entry_min_change", "stop", "take_profit", "trailing", "max_hold"]
    axes = [shapes, entry_min_change, stop, take_profit, trailing, max_hold]
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*axes)]


def _entry_idx(
    shape: str, bars: list[dict[str, Any]], prev_close: float, trigger: float, lo: float, hi: float
) -> int | None:
    if shape == "pullback":
        return reconstruct_pullback_entry(bars, prev_close, trigger, lo, hi)
    if shape == "orb":
        return reconstruct_orb_entry(bars, prev_close, trigger, lo, hi)
    if shape == "gap":
        return reconstruct_gap_and_go_entry(bars, prev_close, trigger, lo, hi)
    return reconstruct_entry(bars, prev_close, trigger, lo, hi)  # "chase"


def evaluate_combo(
    trades: list[TradeInput],
    combo: dict[str, Any],
    min_price: float,
    max_price: float,
    cost_fn: Callable[[float], float] | None,
) -> dict[str, Any] | None:
    """Run one combo over all trade-inputs; aggregate net-return stats (None if
    the combo never enters any of them)."""
    ep = ExitParams(
        stop_loss_pct=combo["stop"],
        take_profit_pct=combo["take_profit"],
        scale_out_fraction=0.0,
        trailing_stop_pct=combo["trailing"],
        max_hold_minutes=combo["max_hold"],
        exit_spread_pct=1.0,
    )
    out = []
    for t in trades:
        idx = _entry_idx(
            combo["shape"], t.bars, t.prev_close, combo["entry_min_change"], min_price, max_price
        )
        if idx is None:
            continue
        tr = simulate_trade(
            t.bars,
            t.prev_close,
            combo["entry_min_change"],
            min_price,
            max_price,
            ep,
            cost_fn=cost_fn,
            entry_idx=idx,
        )
        if tr.entered:
            out.append(tr)
    return aggregate(out)


def search(
    splits: dict[str, list[TradeInput]],
    grid: list[dict[str, Any]],
    min_price: float,
    max_price: float,
    cost_fn: Callable[[float], float] | None,
    min_n: int = 20,
) -> list[dict[str, Any]]:
    """Evaluate every combo on train, keep those with ≥ `min_n` train entries, and
    attach their test stats. Sorted by train avg net return (desc). Holdout is NOT
    touched here — the caller evaluates only the final pick against it."""
    train = splits["train"]
    test = splits.get("test", [])
    scored: list[dict[str, Any]] = []
    for combo in grid:
        tr = evaluate_combo(train, combo, min_price, max_price, cost_fn)
        if tr is None or tr["n"] < min_n:
            continue
        te = evaluate_combo(test, combo, min_price, max_price, cost_fn) if test else None
        scored.append({"combo": combo, "train": tr, "test": te})
    scored.sort(key=lambda r: r["train"]["avg"], reverse=True)
    return scored
