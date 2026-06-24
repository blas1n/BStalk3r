"""Replay simulation: apply alternate filters to stored runners, score by outcome.

Pure — reuses scanner.passes_filters so replay can never diverge from live entry
logic. Rows mimic `get_screened_with_outcomes` output (screened fields + the
forward outcome for one horizon).
"""

from __future__ import annotations

from src.replay import ReplayResult, round_trip_cost, simulate
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


def test_round_trip_cost_adds_cheap_surcharge():
    # base only for a normal-priced name
    assert round_trip_cost(10.0, base_pct=2.0, cheap_price=2.0, cheap_extra_pct=3.0) == 2.0
    # cheap (< threshold) pays base + surcharge
    assert round_trip_cost(1.5, base_pct=2.0, cheap_price=2.0, cheap_extra_pct=3.0) == 5.0


def test_cost_reduces_net_return_and_winrate():
    gross = simulate("gross", ROWS, BASE)
    # flat 5% round-trip on every trade
    net = simulate("net", ROWS, BASE, cost_fn=lambda r: 5.0)
    assert abs(net.avg_return - (gross.avg_return - 5.0)) < 1e-9
    # WIN1 +10 -> +5 (still win), WIN2 +6 -> +1 (win), LOSS -4 -> -9 (loss)
    assert abs(net.win_rate - (2 / 3)) < 1e-9
    # a stiff cost flips marginal winners to losers
    stiff = simulate("stiff", ROWS, BASE, cost_fn=lambda r: 8.0)
    assert abs(stiff.win_rate - (1 / 3)) < 1e-9  # only WIN1 (+10-8=+2) survives


def test_cost_fn_can_be_price_aware():
    # cheaper names cost more; WIN2 is $4 (cheap), WIN1 $8, LOSS $6
    net = simulate("net", ROWS, BASE, cost_fn=lambda r: 5.0 if r["last_price"] < 5 else 1.0)
    by = dict(zip([s for s in net.symbols], [None] * len(net.symbols), strict=False))
    assert "WIN2" in by  # still entered (filter unaffected by cost)
    # WIN1 10-1=9, WIN2 6-5=1, LOSS -4-1=-5 -> avg (9+1-5)/3
    assert abs(net.avg_return - (9 + 1 - 5) / 3) < 1e-9


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
