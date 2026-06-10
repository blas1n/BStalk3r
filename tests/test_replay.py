"""Replay simulation: apply alternate filters to stored runners, score by outcome.

Pure — reuses scanner.passes_filters so replay can never diverge from live entry
logic. Rows mimic `get_screened_with_outcomes` output (screened fields + the
forward outcome for one horizon).
"""

from __future__ import annotations

from src.replay import ReplayResult, simulate
from src.scanner import ScanFilters

# Replay filters: gate on the fields EOD data actually has (price/change/rvol),
# permissive on intraday-only fields (vol-accel/spread).
BASE = ScanFilters(
    min_price=1.0,
    max_price=50.0,
    min_day_change_pct=5.0,
    max_day_change_pct=1000.0,
    min_rvol=8.0,
    min_volume_acceleration=0.0,
    max_spread_pct=1e9,
)


def _row(symbol, price, chg, rvol, ret, max_gain=0.0, max_dd=0.0):
    return {
        "symbol": symbol,
        "last_price": price,
        "day_change_pct": chg,
        "rvol": rvol,
        "volume_acceleration": 1.0,
        "spread_pct": 0.0,
        "ret": ret,
        "max_gain": max_gain,
        "max_drawdown": max_dd,
    }


ROWS = [
    _row("WIN1", 8.0, 20.0, 12.0, ret=10.0, max_gain=18.0, max_dd=-3.0),
    _row("WIN2", 4.0, 30.0, 20.0, ret=6.0, max_gain=9.0, max_dd=-2.0),
    _row("LOSS", 6.0, 10.0, 9.0, ret=-4.0, max_gain=2.0, max_dd=-8.0),
    _row("LOWRV", 5.0, 25.0, 3.0, ret=50.0),  # huge winner but rvol below 8
    _row("CHEAP", 0.5, 40.0, 30.0, ret=99.0),  # below price floor
]


def test_filter_selects_only_qualifying_runners():
    r = simulate("base", ROWS, BASE)
    assert set(r.symbols) == {"WIN1", "WIN2", "LOSS"}  # LOWRV (rvol) + CHEAP (price) excluded
    assert r.n_entered == 3
    assert r.n_scored == 3


def test_metrics_aggregate_outcomes():
    r = simulate("base", ROWS, BASE)
    assert abs(r.avg_return - (10.0 + 6.0 - 4.0) / 3) < 1e-9
    assert abs(r.win_rate - (2 / 3)) < 1e-9
    assert abs(r.avg_max_drawdown - (-3.0 - 2.0 - 8.0) / 3) < 1e-9


def test_raising_rvol_threshold_changes_entry_set():
    strict = ScanFilters(
        min_price=1.0,
        max_price=50.0,
        min_day_change_pct=5.0,
        max_day_change_pct=1000.0,
        min_rvol=15.0,
        min_volume_acceleration=0.0,
        max_spread_pct=1e9,
    )
    r = simulate("rvol>=15", ROWS, strict)
    assert set(r.symbols) == {"WIN2"}  # only rvol 20 passes
    assert r.n_entered == 1


def test_unscored_entries_counted_but_excluded_from_metrics():
    rows = ROWS + [_row("PENDING", 7.0, 22.0, 11.0, ret=None)]
    r = simulate("base", rows, BASE)
    assert r.n_entered == 4  # PENDING passes the filter
    assert r.n_scored == 3  # but has no outcome yet -> not in metrics
    assert abs(r.avg_return - (10.0 + 6.0 - 4.0) / 3) < 1e-9


def test_empty_entry_set_yields_none_metrics():
    impossible = ScanFilters(
        min_price=1000.0,
        max_price=2000.0,
        min_day_change_pct=5.0,
        max_day_change_pct=10.0,
        min_rvol=8.0,
        min_volume_acceleration=0.0,
        max_spread_pct=1e9,
    )
    r = simulate("none", ROWS, impossible)
    assert isinstance(r, ReplayResult)
    assert r.n_entered == 0
    assert r.avg_return is None and r.win_rate is None
