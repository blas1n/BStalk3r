"""Daily portfolio simulator. Pure — no I/O.

Turns per-symbol trade signals into a real account curve: a capacity-capped book
of `max_positions` equal-weight slots, marked daily off each symbol's closes.
Answers the question per-trade stats can't — actual Sharpe / max-drawdown /
capacity of the RSI-2 mean-reversion edge.
"""

from __future__ import annotations

from src.portfolio import simulate_portfolio


def _price(series_by_symbol):
    return series_by_symbol


def test_single_position_tracks_symbol_return():
    # one trade in AAA held d1->d3; max_positions=1 so fully invested in it.
    price = _price({"AAA": {"d1": 10.0, "d2": 11.0, "d3": 12.0}})
    trades = [{"symbol": "AAA", "entry_date": "d1", "exit_date": "d3"}]
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0)
    daily = dict(out["daily"])
    # d1: just opened, no prior mark -> 0; d2: 11/10-1=+10%; d3: 12/11-1=+9.09%
    assert abs(daily["d2"] - 0.10) < 1e-9
    assert abs(daily["d3"] - (12 / 11 - 1)) < 1e-9
    assert out["stats"]["n_trades"] == 1
    # total return = 12/10 - 1 = 20%
    assert abs(out["stats"]["total_return"] - 0.20) < 1e-6


def test_capacity_cap_skips_excess_signals():
    price = _price(
        {
            "AAA": {"d1": 10.0, "d2": 11.0},
            "BBB": {"d1": 10.0, "d2": 11.0},
            "CCC": {"d1": 10.0, "d2": 11.0},
        }
    )
    # 3 signals same day but only 2 slots -> CCC skipped
    trades = [
        {"symbol": "AAA", "entry_date": "d1", "exit_date": "d2"},
        {"symbol": "BBB", "entry_date": "d1", "exit_date": "d2"},
        {"symbol": "CCC", "entry_date": "d1", "exit_date": "d2"},
    ]
    out = simulate_portfolio(price, trades, max_positions=2, cost_frac=0.0)
    assert out["stats"]["n_trades"] == 2  # CCC dropped by capacity
    # d2: two positions each +10%, each weighted 1/2 -> +10% total
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9


def test_half_invested_when_fewer_positions_than_slots():
    price = _price({"AAA": {"d1": 10.0, "d2": 12.0}})  # +20%
    trades = [{"symbol": "AAA", "entry_date": "d1", "exit_date": "d2"}]
    out = simulate_portfolio(price, trades, max_positions=2, cost_frac=0.0)
    # only 1 of 2 slots used -> portfolio earns half: +10%
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9


def test_cost_applied_on_entry_and_exit():
    price = _price({"AAA": {"d1": 10.0, "d2": 10.0, "d3": 10.0}})  # flat
    trades = [{"symbol": "AAA", "entry_date": "d1", "exit_date": "d3"}]
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.01)
    daily = dict(out["daily"])
    # flat price, but entry cost on d1 and exit cost on d3, each -1%
    assert abs(daily["d1"] - (-0.01)) < 1e-9
    assert abs(daily["d3"] - (-0.01)) < 1e-9
    assert abs(daily["d2"]) < 1e-12


def test_stats_include_sharpe_drawdown_capacity():
    price = _price(
        {
            "AAA": {"d1": 10.0, "d2": 11.0, "d3": 9.0, "d4": 10.0},
        }
    )
    trades = [{"symbol": "AAA", "entry_date": "d1", "exit_date": "d4"}]
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0)
    s = out["stats"]
    assert "sharpe" in s and "max_drawdown" in s and "avg_positions" in s
    assert s["max_drawdown"] < 0  # had a drawdown (11 -> 9)
    assert 0 < s["avg_positions"] <= 1


def test_empty_trades_is_flat():
    out = simulate_portfolio({"AAA": {"d1": 10.0, "d2": 11.0}}, [], max_positions=5, cost_frac=0.0)
    assert out["stats"]["n_trades"] == 0
    assert out["stats"]["total_return"] == 0.0
