"""Ranked entry: take the strongest (most-oversold) signals when the book is
full, instead of first-come. Refines the conservative floor from #34."""

from __future__ import annotations

from src.mean_reversion import mean_reversion_trades
from src.portfolio import simulate_portfolio


def test_trade_records_entry_rsi():
    # strong uptrend, 2-day dip at idx 6-7 (RSI-2 = 0), then rips
    closes = [10, 12, 14, 16, 18, 20, 19, 18.5, 22.0]
    dates = [f"2026-06-{d:02d}" for d in range(1, len(closes) + 1)]
    dvols = [1e8] * len(closes)
    trades = mean_reversion_trades(
        dates,
        [float(c) for c in closes],
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
    assert "entry_rsi" in trades[0]
    assert trades[0]["entry_rsi"] <= 10  # oversold at entry


def test_portfolio_rank_key_admits_strongest_under_capacity():
    price = {
        "WEAK": {"d1": 10.0, "d2": 10.5},  # weaker signal (higher RSI)
        "STRONG": {"d1": 10.0, "d2": 11.0},  # stronger signal (lower RSI)
    }
    # both fire on d1 but only 1 slot; rank_key='entry_rsi' ascending -> STRONG wins
    trades = [
        {"symbol": "WEAK", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 9.0},
        {"symbol": "STRONG", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 1.0},
    ]
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0, rank_key="entry_rsi")
    assert out["stats"]["n_trades"] == 1
    # STRONG (+10%) admitted, not WEAK (+5%) -> d2 return +10%
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9


def test_portfolio_no_rank_key_is_first_come():
    price = {"A": {"d1": 10.0, "d2": 11.0}, "B": {"d1": 10.0, "d2": 12.0}}
    trades = [
        {"symbol": "A", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 5.0},
        {"symbol": "B", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 1.0},
    ]
    # default (no rank) -> first-come admits A (order preserved), B dropped
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0)
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9  # A's +10%, not B's +20%
