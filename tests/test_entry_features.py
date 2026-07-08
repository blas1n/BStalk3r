"""Entry-time features: everything observable AT the +X% cross moment.

Computed only from the minute bars UP TO AND INCLUDING the entry bar (no
look-ahead) — these are the signals a real-time scanner could act on to try to
separate future winners from fizzles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.intraday import entry_features

T0 = datetime(2026, 7, 2, 13, 30, tzinfo=UTC)  # 09:30 ET open


def _bars(rows):
    # rows: (close, volume); high=low=open=close for simplicity
    out = []
    for i, (c, v) in enumerate(rows):
        out.append(
            {
                "ts": T0 + timedelta(minutes=i),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": v,
            }
        )
    return out


def test_features_use_only_bars_up_to_entry():
    # open 10.0, crosses +5% (>=10.5) at idx 2; bars after entry must be ignored
    bars = _bars([(10.0, 100), (10.2, 200), (10.6, 300), (20.0, 9999)])
    f = entry_features(bars, entry_idx=2, prev_close=10.0)
    assert f["cum_volume"] == 600  # 100+200+300, NOT the post-entry 9999
    assert f["minutes_to_cross"] == 2  # crossed on the 3rd bar (index 2)
    assert abs(f["entry_price"] - 10.6) < 1e-9


def test_gap_pct_from_open_vs_prev_close():
    bars = _bars([(10.5, 100), (10.6, 100)])  # opened at 10.5, prev 10.0 -> +5% gap
    f = entry_features(bars, entry_idx=0, prev_close=10.0)
    assert abs(f["gap_pct"] - 5.0) < 1e-9


def test_vol_accel_last_minute_vs_running_avg():
    bars = _bars([(10.6, 100), (10.7, 100), (10.8, 400)])  # last min 4x the avg
    f = entry_features(bars, entry_idx=2, prev_close=10.0)
    # avg of [100,100,400]=200; last=400 -> 2.0x
    assert abs(f["vol_accel"] - 2.0) < 1e-9


def test_dollar_volume_and_price_level():
    bars = _bars([(2.0, 1000), (2.1, 1000)])
    f = entry_features(bars, entry_idx=1, prev_close=2.0)
    assert abs(f["entry_price"] - 2.1) < 1e-9
    assert abs(f["cum_dollar_vol"] - 2000 * 2.1) < 1e-6  # 2000 shares * $2.1


def test_zero_entry_idx_safe():
    bars = _bars([(10.6, 100)])
    f = entry_features(bars, entry_idx=0, prev_close=10.0)
    assert f["minutes_to_cross"] == 0
    assert f["vol_accel"] == 1.0  # only one bar
