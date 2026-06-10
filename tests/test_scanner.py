"""Scanner: universe filtering + ranking. Pure, no external calls."""

from __future__ import annotations

from src.scanner import ScanFilters, passes_filters, scan_candidates, score_candidate

from tests.conftest import make_snapshot

FILTERS = ScanFilters(
    min_price=1.0,
    max_price=50.0,
    min_day_change_pct=5.0,
    max_day_change_pct=40.0,
    min_rvol=8.0,
    min_volume_acceleration=3.0,
    max_spread_pct=1.0,
)


def test_clean_runner_passes():
    assert passes_filters(make_snapshot(), FILTERS) is True


def test_price_below_floor_rejected():
    assert passes_filters(make_snapshot(last_price=0.80), FILTERS) is False


def test_price_above_cap_rejected():
    assert passes_filters(make_snapshot(last_price=75.0), FILTERS) is False


def test_day_change_too_small_rejected():
    assert passes_filters(make_snapshot(day_change_pct=3.0), FILTERS) is False


def test_day_change_too_large_rejected():
    # 40%+ is "already gone" — outside the runnable window.
    assert passes_filters(make_snapshot(day_change_pct=55.0), FILTERS) is False


def test_low_rvol_rejected():
    assert passes_filters(make_snapshot(rvol=4.0), FILTERS) is False


def test_no_volume_acceleration_rejected():
    assert passes_filters(make_snapshot(volume_acceleration=1.5), FILTERS) is False


def test_wide_spread_rejected():
    assert passes_filters(make_snapshot(spread_pct=2.5), FILTERS) is False


def test_float_data_absent_still_passes():
    # float is nullable in v0 — strategy must not require it.
    assert passes_filters(make_snapshot(float_shares=None), FILTERS) is True


def test_scan_filters_and_ranks_by_score():
    weak = make_snapshot(symbol="WEAK", rvol=8.1, volume_acceleration=3.1, day_change_pct=5.1)
    strong = make_snapshot(
        symbol="STRONG", rvol=30.0, volume_acceleration=12.0, day_change_pct=25.0
    )
    rejected = make_snapshot(symbol="NOPE", rvol=2.0)

    out = scan_candidates([weak, rejected, strong], FILTERS)

    assert [s.symbol for s in out] == ["STRONG", "WEAK"]  # rejected dropped, strongest first


def test_score_is_monotonic_in_rvol():
    lo = score_candidate(make_snapshot(rvol=8.0))
    hi = score_candidate(make_snapshot(rvol=20.0))
    assert hi > lo
