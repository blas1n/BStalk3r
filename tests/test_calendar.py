"""Calendar/seasonality engine — Turn-of-Month. Pure — no I/O.

Structural (not arbitrage) driver: month-end institutional rebalancing. Long an
equal-weight liquid basket only during the TOM window (last N trading days of a
month + first M of the next), flat otherwise. Research: TOM is the only
persistently significant calendar effect.
"""

from __future__ import annotations

from src.calendar_strat import (
    calendar_trades,
    classify_tom,
    equal_weight_returns,
    summarize_trades,
)


def test_classify_tom_last_and_first_trading_days():
    # full-month data (first entries per month = the month's real first days)
    dates = [
        "2026-01-02",
        "2026-01-05",  # Jan first 2
        "2026-01-29",
        "2026-01-30",  # Jan last 2
        "2026-02-02",
        "2026-02-03",  # Feb first 2
        "2026-02-26",
        "2026-02-27",  # Feb last 2
    ]
    win = classify_tom(dates, days_before=1, days_after=2)
    assert "2026-01-30" in win  # last 1 of Jan
    assert "2026-02-02" in win and "2026-02-03" in win  # first 2 of Feb
    assert "2026-01-05" in win  # first 2 of Jan (month-start side)
    assert "2026-01-29" not in win  # 2nd-to-last (days_before=1 -> last 1 only)
    assert "2026-02-26" not in win  # 2nd-to-last of Feb


def test_equal_weight_returns_mean_across_symbols():
    panel = {
        "2026-06-01": {
            "A": {"close": 10.0, "dollar_vol": 1e8},
            "B": {"close": 20.0, "dollar_vol": 1e8},
        },
        "2026-06-02": {
            "A": {"close": 11.0, "dollar_vol": 1e8},
            "B": {"close": 21.0, "dollar_vol": 1e8},
        },
    }
    ew = equal_weight_returns(panel, min_price=1, max_price=100, min_dollar_vol=0)
    assert len(ew) == 1
    d, r = ew[0]
    assert d == "2026-06-02"
    # A: +10%, B: +5% -> mean +7.5%
    assert abs(r - 0.075) < 1e-9


def test_equal_weight_filters_illiquid_and_out_of_band():
    panel = {
        "2026-06-01": {
            "A": {"close": 10.0, "dollar_vol": 1e8},
            "P": {"close": 0.5, "dollar_vol": 1e8},
        },
        "2026-06-02": {
            "A": {"close": 12.0, "dollar_vol": 1e8},
            "P": {"close": 0.6, "dollar_vol": 1e8},
        },
    }
    ew = equal_weight_returns(panel, min_price=1, max_price=100, min_dollar_vol=0)
    # P (<$1) excluded -> only A's +20%
    assert abs(ew[0][1] - 0.20) < 1e-9


def test_calendar_trades_compound_runs_net_of_cost():
    ew = [
        ("2026-01-30", 0.01),  # window run 1 (single day)
        ("2026-02-10", 0.05),  # non-window gap (ignored)
        ("2026-02-26", 0.02),  # window run 2 starts
        ("2026-02-27", -0.01),  # ...continues
    ]
    window = {"2026-01-30", "2026-02-26", "2026-02-27"}
    trades = calendar_trades(ew, window, cost_frac=0.001)
    assert len(trades) == 2
    # run1: 1.01 - 1 - 2*0.001 = 0.008
    assert abs(trades[0]["ret"] - (0.01 - 0.002)) < 1e-9
    # run2: 1.02*0.99 - 1 - 0.002
    assert abs(trades[1]["ret"] - ((1.02 * 0.99 - 1) - 0.002)) < 1e-9
    assert trades[1]["days"] == 2


def test_summarize_trades_and_empty():
    assert summarize_trades([]) is None
    s = summarize_trades([{"ret": 0.02}, {"ret": -0.01}, {"ret": 0.03}])
    assert s["n"] == 3
    assert abs(s["avg"] - (0.04 / 3)) < 1e-9
    assert abs(s["win"] - (2 / 3) * 100) < 1e-9
