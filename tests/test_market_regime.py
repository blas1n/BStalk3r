"""Market vol-regime filter (VIX proxy). Pure — no I/O.

RSI-2 mean reversion buys dips; in a high-volatility crash it buys falling knives.
Literature: gating entries to calm regimes lifts the profit factor. We proxy VIX
with SPY's rolling realized volatility (free daily data, already in the panel) and
only allow entries when it's below a threshold.
"""

from __future__ import annotations

from src.market_regime import calm_dates, realized_vol


def test_realized_vol_annualized_rolling():
    closes = [100.0, 110.0, 99.0, 108.9]  # returns +10%, -10%, +10%
    vol = realized_vol(closes, window=2)
    assert vol[0] is None and vol[1] is None  # warmup
    # window of returns [0.1, -0.1]: pop std 0.1, annualized 0.1*sqrt(252)
    assert abs(vol[2] - 0.1 * (252**0.5)) < 1e-6


def test_calm_dates_gates_on_threshold():
    # flat then a volatile stretch
    closes = [100.0, 100.5, 100.0, 100.5, 120.0, 96.0, 115.0]
    dates = [f"2026-06-{d:02d}" for d in range(1, 8)]
    calm = calm_dates(dates, closes, window=2, max_vol=0.30)
    # early low-vol days qualify; the wild swings (idx 4-6) blow past 30% ann vol
    assert "2026-06-03" in calm or "2026-06-04" in calm
    assert "2026-06-06" not in calm  # after the +20%/-20% swings
    # warmup dates (before `window` returns) are excluded (unknown regime)
    assert "2026-06-01" not in calm


def test_calm_dates_all_when_flat():
    closes = [100.0] * 10  # zero vol
    dates = [f"2026-06-{d:02d}" for d in range(1, 11)]
    calm = calm_dates(dates, closes, window=3, max_vol=0.20)
    # every post-warmup day is calm (vol 0)
    assert "2026-06-05" in calm and "2026-06-10" in calm
