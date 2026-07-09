"""Long-strategy search core. Pure — no I/O.

Runs a grid of (entry shape × params) over the honest crosser universe with a
chronological train/test split, so we can see whether a train winner survives
out-of-sample instead of manufacturing an overfit mirage. Holdout is evaluated
once by the caller for the single final pick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.long_search import TradeInput, evaluate_combo, make_grid, search

T0 = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)


def _bars(rows):  # (high, low, close, vol)
    return [
        {"ts": T0 + timedelta(minutes=i), "open": c, "high": h, "low": low, "close": c, "volume": v}
        for i, (h, low, c, v) in enumerate(rows)
    ]


def _chase_winner(sym="W"):
    # activates +10% then runs straight up -> chase enters & profits
    return TradeInput(
        sym, "2026-07-06", 10.0, _bars([(10, 10, 10, 100), (11, 11, 11, 500), (13, 13, 13, 500)])
    )


def _chase_loser(sym="L"):
    # activates +10% then collapses -> chase enters & loses (stop)
    return TradeInput(
        sym, "2026-07-06", 10.0, _bars([(10, 10, 10, 100), (11, 11, 11, 500), (9.5, 9.4, 9.5, 500)])
    )


_CHASE = {
    "shape": "chase",
    "entry_min_change": 5.0,
    "stop": 0.05,
    "take_profit": 0.15,
    "trailing": 0.08,
    "max_hold": 180.0,
}


def test_evaluate_combo_aggregates_entered_trades():
    stats = evaluate_combo(
        [_chase_winner(), _chase_loser()], _CHASE, min_price=1.0, max_price=50.0, cost_fn=None
    )
    assert stats is not None
    assert stats["n"] == 2  # both activate & enter
    assert stats["win_rate"] == 50.0  # one up, one down


def test_evaluate_combo_none_when_no_entries():
    flat = TradeInput("F", "2026-07-06", 10.0, _bars([(10, 10, 10, 100), (10.1, 10.1, 10.1, 100)]))
    assert evaluate_combo([flat], _CHASE, 1.0, 50.0, None) is None


def test_make_grid_expands_all_combos():
    grid = make_grid(
        shapes=["chase", "pullback"],
        entry_min_change=[5.0, 10.0],
        stop=[0.05],
        take_profit=[0.10, 0.15],
        trailing=[0.08],
        max_hold=[60.0, 180.0],
    )
    assert len(grid) == 2 * 2 * 1 * 2 * 1 * 2  # 16
    assert all(
        {"shape", "entry_min_change", "stop", "take_profit", "trailing", "max_hold"} <= c.keys()
        for c in grid
    )


def test_search_ranks_by_train_and_reports_test(monkeypatch):
    # train: 2 winners + 1 loser (chase positive). test: 1 winner.
    splits = {
        "train": [_chase_winner("W1"), _chase_winner("W2"), _chase_loser("L1")],
        "test": [_chase_winner("T1")],
    }
    grid = make_grid(
        shapes=["chase"],
        entry_min_change=[5.0],
        stop=[0.05],
        take_profit=[0.15],
        trailing=[0.08],
        max_hold=[180.0],
    )
    results = search(splits, grid, min_price=1.0, max_price=50.0, cost_fn=None, min_n=1)
    assert len(results) == 1
    r = results[0]
    assert r["train"]["n"] == 3 and r["test"]["n"] == 1
    assert r["train"]["avg"] is not None
    assert r["combo"]["shape"] == "chase"


def test_search_filters_low_sample_combos():
    splits = {"train": [_chase_winner()], "test": []}
    grid = make_grid(
        shapes=["chase"],
        entry_min_change=[5.0],
        stop=[0.05],
        take_profit=[0.15],
        trailing=[0.08],
        max_hold=[180.0],
    )
    # min_n=5 but only 1 train entry -> combo dropped
    assert search(splits, grid, 1.0, 50.0, None, min_n=5) == []
