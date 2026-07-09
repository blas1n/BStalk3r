"""Pullback / VWAP-reclaim long entry. Pure — no I/O.

The chase long (buy the +X% cross) is falsified. The literature's short-term
reversal points the other way: after a runner activates, don't chase it — wait
for the pullback and buy the *reclaim* (price dips to/under intraday VWAP, then
closes back above it). Same live exit rules; only the entry differs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.intraday import (
    reconstruct_pullback_entry,
    simulate_trade,
    vwap_series,
)
from src.strategy import ExitParams

T0 = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)

EXIT = ExitParams(
    stop_loss_pct=0.05,
    take_profit_pct=0.15,
    scale_out_fraction=0.5,
    trailing_stop_pct=0.08,
    max_hold_minutes=180,
    exit_spread_pct=1.5,
)


def _bars(rows):  # (high, low, close, volume)
    return [
        {
            "ts": T0 + timedelta(minutes=i),
            "open": c,
            "high": h,
            "low": low,
            "close": c,
            "volume": v,
        }
        for i, (h, low, c, v) in enumerate(rows)
    ]


def test_vwap_series_is_volume_weighted_running_typical_price():
    bars = _bars([(10, 10, 10, 100), (12, 12, 12, 300)])
    vw = vwap_series(bars)
    assert abs(vw[0] - 10.0) < 1e-9
    # cum(typical*vol)/cum(vol) = (10*100 + 12*300)/400 = 11.5
    assert abs(vw[1] - 11.5) < 1e-9


def test_pullback_entry_buys_the_vwap_reclaim_after_activation():
    # activates at +5% over prev_close 10 (>=10.5) at idx1; dips below VWAP at
    # idx2, reclaims (closes back above VWAP) at idx3 -> enter there.
    bars = _bars(
        [
            (10.0, 10.0, 10.0, 100),  # 0 pre
            (11.0, 11.0, 11.0, 500),  # 1 activation (+10%)
            (10.4, 10.2, 10.3, 400),  # 2 pullback (below VWAP)
            (11.2, 10.9, 11.1, 600),  # 3 reclaim -> entry
            (12.0, 11.5, 12.0, 400),  # 4
        ]
    )
    idx = reconstruct_pullback_entry(
        bars, prev_close=10.0, entry_min_change=5.0, min_price=1.0, max_price=50.0
    )
    assert idx == 3


def test_no_pullback_entry_if_never_activates():
    bars = _bars([(10.0, 10.0, 10.0, 100), (10.2, 10.1, 10.2, 100)])  # never +5%
    assert reconstruct_pullback_entry(bars, 10.0, 5.0, 1.0, 50.0) is None


def test_no_pullback_entry_if_never_dips_below_vwap():
    # activates then only runs up — no pullback -> no reclaim -> no entry
    bars = _bars([(10, 10, 10, 100), (11, 11, 11, 500), (12, 12, 12, 500), (13, 13, 13, 500)])
    assert reconstruct_pullback_entry(bars, 10.0, 5.0, 1.0, 50.0) is None


def test_simulate_trade_honors_injected_entry_idx():
    # inject entry at idx 2 (close 10.3); price then runs to take-profit
    bars = _bars(
        [(10, 10, 10, 100), (11, 11, 11, 100), (10.4, 10.2, 10.3, 100), (12.0, 11.9, 12.0, 100)]
    )
    t = simulate_trade(
        bars,
        10.0,
        entry_min_change=99.0,
        min_price=1.0,
        max_price=50.0,
        exit_params=EXIT,
        entry_idx=2,
    )
    assert t.entered is True
    assert abs(t.entry_price - 10.3) < 1e-9
    assert t.gross_return_pct > 0  # bought 10.3, rose
