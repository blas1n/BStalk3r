"""Forward-outcome computation. Pure: forward bars -> returns / max-gain / MDD."""

from __future__ import annotations

from src.forward_bars import polygon_aggs_to_bars
from src.outcomes import compute_outcomes

# 5 forward sessions after a runner closed at ref 10.0
BARS = [
    {"high": 11.0, "low": 9.5, "close": 10.5},
    {"high": 12.0, "low": 10.0, "close": 11.5},
    {"high": 11.8, "low": 10.5, "close": 11.0},
    {"high": 13.0, "low": 10.8, "close": 12.5},
    {"high": 12.0, "low": 9.0, "close": 9.5},
]


def _by(outs):
    return {o["horizon"]: o for o in outs}


def test_forward_return_at_each_horizon():
    out = _by(compute_outcomes(10.0, BARS, horizons=(1, 3, 5)))
    assert abs(out["1d"]["fwd_return_pct"] - 5.0) < 1e-6  # close 10.5
    assert abs(out["3d"]["fwd_return_pct"] - 10.0) < 1e-6  # close 11.0
    assert abs(out["5d"]["fwd_return_pct"] - (-5.0)) < 1e-6  # close 9.5


def test_max_gain_is_window_high_vs_ref():
    out = _by(compute_outcomes(10.0, BARS, horizons=(1, 3, 5)))
    assert abs(out["1d"]["max_gain_pct"] - 10.0) < 1e-6  # high 11
    assert abs(out["3d"]["max_gain_pct"] - 20.0) < 1e-6  # high 12 in first 3
    assert abs(out["5d"]["max_gain_pct"] - 30.0) < 1e-6  # high 13


def test_max_drawdown_is_window_low_vs_ref():
    out = _by(compute_outcomes(10.0, BARS, horizons=(1, 3, 5)))
    assert abs(out["1d"]["max_drawdown_pct"] - (-5.0)) < 1e-6  # low 9.5
    assert abs(out["5d"]["max_drawdown_pct"] - (-10.0)) < 1e-6  # low 9.0


def test_horizon_skipped_when_not_enough_forward_bars():
    out = _by(compute_outcomes(10.0, BARS[:2], horizons=(1, 3, 5)))
    assert "1d" in out
    assert "3d" not in out and "5d" not in out  # only 2 bars available


def test_ref_and_fwd_prices_recorded():
    out = _by(compute_outcomes(10.0, BARS, horizons=(1,)))
    assert out["1d"]["ref_price"] == 10.0
    assert out["1d"]["fwd_price"] == 10.5


def test_empty_bars_yields_nothing():
    assert compute_outcomes(10.0, [], horizons=(1, 3, 5)) == []


def test_zero_ref_is_safe():
    assert compute_outcomes(0.0, BARS, horizons=(1,)) == []


def test_polygon_aggs_normalizes_and_sorts():
    payload = {
        "results": [
            {"t": 2, "h": 11.0, "l": 9.0, "c": 10.0},
            {"t": 1, "h": 12.0, "l": 8.0, "c": 9.0},
            {"t": 3, "h": None, "l": 1.0, "c": 1.0},  # incomplete -> skipped
        ]
    }
    bars = polygon_aggs_to_bars(payload)
    assert [b["close"] for b in bars] == [9.0, 10.0]  # sorted by ts, incomplete dropped
    assert bars[0]["high"] == 12.0


def test_polygon_aggs_empty():
    assert polygon_aggs_to_bars({"status": "OK"}) == []
