"""Cross-sectional daily reversal/momentum engine. Pure — no I/O.

Broadens beyond low-float spikes: rank a *liquid* daily universe by recent
formation return, long one tail and short the other, hold N days. Reversal (long
losers / short winners) and momentum (the inverse) in one engine. All from
grouped daily bars (cheap, cached), long+short (liquid = borrowable).
"""

from __future__ import annotations

from src.xsectional import (
    RebalanceResult,
    build_panel,
    cross_sectional_backtest,
    summarize_rebalances,
)


def _grouped(close_by_symbol, vol=1_000_000):
    return [{"T": sym, "c": c, "v": vol} for sym, c in close_by_symbol.items()]


# A: winner (10->12 formation, then 12->11 fade). B: loser (10->8, then 8->9 bounce).
PANEL_SRC = {
    "2026-06-01": _grouped({"A": 10.0, "B": 10.0}),
    "2026-06-02": _grouped({"A": 12.0, "B": 8.0}),  # rebalance date
    "2026-06-03": _grouped({"A": 11.0, "B": 9.0}),
}


def test_build_panel_indexes_by_date_then_symbol():
    panel = build_panel(PANEL_SRC)
    assert sorted(panel) == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert panel["2026-06-02"]["A"]["close"] == 12.0
    assert panel["2026-06-02"]["A"]["dollar_vol"] == 12.0 * 1_000_000


def test_reversal_longs_losers_shorts_winners():
    panel = build_panel(PANEL_SRC)
    res = cross_sectional_backtest(
        panel,
        strategy="reversal",
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1.0,
        max_price=100.0,
        min_dollar_vol=0.0,
    )
    assert len(res) == 1
    r = res[0]
    assert r.date == "2026-06-02" and r.n_long == 1 and r.n_short == 1
    # long basket = B (loser) fwd = 9/8-1 = +12.5%; short basket = A (winner) fwd = 11/12-1 = -8.3%
    assert abs(r.long_ret - 0.125) < 1e-6
    assert abs(r.short_ret - (-1 / 12)) < 1e-6


def test_momentum_is_the_inverse_assignment():
    panel = build_panel(PANEL_SRC)
    rev = cross_sectional_backtest(
        panel,
        strategy="reversal",
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1.0,
        max_price=100.0,
        min_dollar_vol=0.0,
    )[0]
    mom = cross_sectional_backtest(
        panel,
        strategy="momentum",
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1.0,
        max_price=100.0,
        min_dollar_vol=0.0,
    )[0]
    # momentum long/short baskets are swapped vs reversal
    assert mom.long_ret == rev.short_ret and mom.short_ret == rev.long_ret


def test_liquidity_and_band_filters_exclude_symbols():
    panel = build_panel(
        {
            "2026-06-01": _grouped({"A": 10.0, "PENNY": 0.5, "PRICEY": 500.0}),
            "2026-06-02": _grouped({"A": 12.0, "PENNY": 0.6, "PRICEY": 520.0}),
            "2026-06-03": _grouped({"A": 11.0, "PENNY": 0.55, "PRICEY": 510.0}),
        }
    )
    res = cross_sectional_backtest(
        panel,
        strategy="reversal",
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1.0,
        max_price=100.0,
        min_dollar_vol=0.0,
    )
    # PENNY (<$1) and PRICEY (>$100) filtered -> only A eligible -> can't form both
    # tails from 1 symbol -> no rebalance emitted
    assert res == []


def test_summarize_applies_costs_to_both_legs():
    results = [
        RebalanceResult(date="d1", long_ret=0.10, short_ret=-0.05, n_long=1, n_short=1),
        RebalanceResult(date="d2", long_ret=0.02, short_ret=0.04, n_long=1, n_short=1),
    ]
    # per-rebalance pnl = long_ret - short_ret; net subtracts 2*cost (both legs)
    gross = summarize_rebalances(results, cost_frac=0.0)
    net = summarize_rebalances(results, cost_frac=0.01)
    # d1 pnl = 0.15, d2 pnl = -0.02 -> gross avg = 0.065
    assert abs(gross["avg"] - 0.065) < 1e-9
    assert abs((gross["avg"] - net["avg"]) - 0.02) < 1e-9  # 2 * 0.01
    assert gross["n"] == 2


def test_summarize_empty_is_none():
    assert summarize_rebalances([], cost_frac=0.0) is None
