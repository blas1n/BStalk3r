"""Short-term mean-reversion engine (Connors RSI-2 style). Pure — no I/O.

Untested technical family (behavioral overreaction, not cross-sectional ranking):
per symbol, buy a 1-2 day panic (RSI-`n` oversold) *while in an uptrend* (close >
long SMA), exit on the bounce (RSI back above a threshold) or max-hold. Literature
puts win rate 75-79% on liquid names; regime filter is what keeps it alive in
downtrends.
"""

from __future__ import annotations

from src.mean_reversion import mean_reversion_trades, rsi, sma, summarize_mr


def test_rsi_period2_known_values():
    closes = [10.0, 11.0, 10.0, 12.0]
    r = rsi(closes, 2)
    assert r[0] is None and r[1] is None  # warmup
    assert abs(r[2] - 50.0) < 1e-6  # gains(1,0)/losses(0,1) -> RS=1 -> 50
    assert abs(r[3] - (100 - 100 / 3)) < 1e-6  # gains(0,2)/losses(1,0) -> RS=2 -> 66.67


def test_rsi_two_down_days_is_fully_oversold():
    r = rsi([10.0, 9.0, 8.0], 2)
    assert r[2] == 0.0  # only losses -> RSI 0


def test_sma_trailing_window():
    s = sma([10.0, 12.0, 14.0, 16.0], 3)
    assert s[0] is None and s[1] is None
    assert abs(s[2] - 12.0) < 1e-9  # mean(10,12,14)
    assert abs(s[3] - 14.0) < 1e-9  # mean(12,14,16)


def _uptrend_with_dip():
    # strong uptrend, a 2-day dip at idx 6-7 that stays above the 5-SMA, then rips
    closes = [10, 12, 14, 16, 18, 20, 19, 18.5, 22.0]
    dates = [f"2026-06-{d:02d}" for d in range(1, len(closes) + 1)]
    dvols = [1e8] * len(closes)
    return dates, [float(c) for c in closes], dvols


def test_enters_oversold_dip_in_uptrend_exits_on_bounce():
    dates, closes, dvols = _uptrend_with_dip()
    trades = mean_reversion_trades(
        dates,
        closes,
        dvols,
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1,
        max_price=1000,
        min_dollar_vol=0,
        cost_frac=0.0,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t["entry_date"] == "2026-06-08"  # idx 7, RSI-2 oversold, close 18.5 > SMA5
    assert abs(t["entry_price"] - 18.5) < 1e-9
    assert abs(t["exit_price"] - 22.0) < 1e-9  # RSI back > 50 next day
    assert t["ret"] > 0.18  # +18.9%


def test_regime_filter_blocks_entry_below_ma():
    # same dip but a DOWNTREND: oversold, yet close is below the SMA -> no entry
    closes = [30, 28, 26, 24, 22, 20, 19, 18.5, 19.0]
    dates = [f"2026-06-{d:02d}" for d in range(1, len(closes) + 1)]
    dvols = [1e8] * len(closes)
    trades = mean_reversion_trades(
        dates,
        closes,
        dvols,
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1,
        max_price=1000,
        min_dollar_vol=0,
        cost_frac=0.0,
    )
    assert trades == []  # regime filter (close < SMA) blocks the dip-buy


def test_liquidity_filter_blocks_illiquid_entry():
    dates, closes, dvols = _uptrend_with_dip()
    dvols[7] = 1_000  # entry bar illiquid
    trades = mean_reversion_trades(
        dates,
        closes,
        dvols,
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1,
        max_price=1000,
        min_dollar_vol=1_000_000,
        cost_frac=0.0,
    )
    assert trades == []


def test_cost_reduces_trade_return():
    dates, closes, dvols = _uptrend_with_dip()
    kw = dict(
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1,
        max_price=1000,
        min_dollar_vol=0,
    )
    gross = mean_reversion_trades(dates, closes, dvols, cost_frac=0.0, **kw)[0]
    net = mean_reversion_trades(dates, closes, dvols, cost_frac=0.01, **kw)[0]
    assert abs((gross["ret"] - net["ret"]) - 0.02) < 1e-9  # round-trip = 2*cost


def test_summarize_mr_stats_and_empty():
    assert summarize_mr([]) is None
    s = summarize_mr([{"ret": 0.10}, {"ret": -0.04}, {"ret": 0.06}])
    assert s["n"] == 3
    assert abs(s["avg"] - 0.04) < 1e-9
    assert abs(s["win"] - (2 / 3) * 100) < 1e-9
