"""First-red-day / multi-day exhaustion short (H-B) — daily concept test.

A stock that ran up parabolically over N days, then closes RED the next day
(first red day), is the practitioner short setup (Temiz/Williams/Verma) and the
academic MAX/lottery-reversal target. Pure over per-symbol daily sequences.
"""

from __future__ import annotations

from src.exhaustion import find_exhaustion_shorts


def _d(date, o, h, low, c):
    return {"date": date, "open": o, "high": h, "low": low, "close": c}


def test_parabolic_then_red_day_is_a_short_setup():
    # RUNR: 10 -> 12 -> 15 (+50% over 2d), then red day opens 15, fades to 12
    seq = {
        "RUNR": [
            _d("2026-07-01", 10, 10, 10, 10.0),
            _d("2026-07-02", 12, 12, 12, 12.0),
            _d("2026-07-03", 15, 15, 15, 15.0),
            _d("2026-07-06", 15.0, 15.2, 11.5, 12.0),  # first red day, fades
            _d("2026-07-07", 12.0, 12.0, 9.0, 9.5),  # continues down
        ]
    }
    out = find_exhaustion_shorts(
        seq, run_days=2, run_min_gain_pct=40.0, min_price=1, max_price=50, fwd_days=1, cost_pct=0.0
    )
    assert len(out) == 1
    s = out[0]
    assert s.symbol == "RUNR" and s.red_day_date == "2026-07-06"
    # intraday short: open 15 -> close 12 = +20% for a short
    assert abs(s.intraday_short_ret - 20.0) < 1e-6
    # swing short: red close 12 -> next close 9.5 = +20.8%
    assert s.swing_short_ret is not None and s.swing_short_ret > 15


def test_no_setup_if_next_day_green():
    seq = {
        "X": [
            _d("2026-07-01", 10, 10, 10, 10.0),
            _d("2026-07-02", 15, 15, 15, 15.0),
            _d("2026-07-03", 15, 16, 15, 16.0),  # green, not a red day
        ]
    }
    out = find_exhaustion_shorts(
        seq, run_days=1, run_min_gain_pct=40, min_price=1, max_price=50, fwd_days=1, cost_pct=0.0
    )
    assert out == []


def test_no_setup_if_run_too_small():
    seq = {
        "X": [
            _d("2026-07-01", 10, 10, 10, 10.0),
            _d("2026-07-02", 10.5, 10.5, 10.5, 10.5),  # +5% only
            _d("2026-07-03", 10.5, 10.5, 10.0, 10.2),  # red but no parabolic run
        ]
    }
    out = find_exhaustion_shorts(
        seq, run_days=1, run_min_gain_pct=40, min_price=1, max_price=50, fwd_days=1, cost_pct=0.0
    )
    assert out == []


def test_band_filter_on_run_end_price():
    seq = {
        "PRICEY": [
            _d("2026-07-01", 100, 100, 100, 100.0),
            _d("2026-07-02", 160, 160, 160, 160.0),  # ran but > $50 band
            _d("2026-07-03", 160, 160, 140, 150.0),
        ]
    }
    out = find_exhaustion_shorts(
        seq, run_days=1, run_min_gain_pct=40, min_price=1, max_price=50, fwd_days=1, cost_pct=0.0
    )
    assert out == []


def test_cost_reduces_short_returns():
    seq = {
        "R": [
            _d("2026-07-01", 10, 10, 10, 10.0),
            _d("2026-07-02", 15, 15, 15, 15.0),
            _d("2026-07-03", 15, 15, 11, 12.0),  # red, intraday fade 20%
        ]
    }
    out = find_exhaustion_shorts(
        seq, run_days=1, run_min_gain_pct=40, min_price=1, max_price=50, fwd_days=1, cost_pct=3.0
    )
    assert abs(out[0].intraday_short_ret - (20.0 - 3.0)) < 1e-6
    assert out[0].swing_short_ret is None  # no forward day available
