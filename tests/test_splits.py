"""Split adjustment of grouped-cache closes (#47) — pure heuristic + research parity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from src.mean_reversion import mean_reversion_trades
from src.splits import SPLIT_K, SPLIT_R, split_adjust_closes, split_adjust_panel

_CORE = Path(__file__).resolve().parents[1] / "research" / "tiebreak" / "core.py"


def _series(n: int = 40, split_at: int = 21, factor: float = 0.1) -> list[float]:
    """A gently rising stock whose price is multiplied by `factor` from bar
    `split_at` on (0.1 = the cache's fake -90% day; 10 = a 1:10 reverse split)."""
    out = []
    for t in range(n):
        px = 20.0 + 0.1 * t
        out.append(round(px * (factor if t >= split_at else 1.0), 4))
    return out


@pytest.mark.parametrize("factor", [10.0, 0.1], ids=["reverse_1_for_10", "forward_10_for_1"])
def test_split_is_back_adjusted_and_reported(factor):
    raw = _series(factor=factor)
    adj, events = split_adjust_closes(raw)
    assert [(i, best) for i, _, best in events] == [(21, factor)]
    # the pre-split history is restated in post-split units; post-split bars untouched
    assert adj[21:] == raw[21:]
    assert adj[20] == pytest.approx(raw[20] * factor)
    assert max(abs(b / a - 1) for a, b in zip(adj, adj[1:], strict=False)) < 0.01


@pytest.mark.parametrize("factor", [10.0, 0.1], ids=["reverse_1_for_10", "forward_10_for_1"])
def test_backtest_on_adjusted_closes_books_no_split_trade(factor):
    raw = _series(factor=factor)
    dates = [f"d{t:03d}" for t in range(len(raw))]
    kw = dict(entry_rsi=100, exit_rsi=101, ma_period=0, max_hold=5, min_price=0.0)
    raw_trades = mean_reversion_trades(dates, raw, [1e9] * len(raw), **kw)
    assert max(abs(t["ret"]) for t in raw_trades) > 0.85  # the bug: split booked as P&L
    adj, _ = split_adjust_closes(raw)
    adj_trades = mean_reversion_trades(dates, adj, [1e9] * len(adj), **kw)
    assert max(abs(t["ret"]) for t in adj_trades) < 0.05


def test_real_moves_and_off_ratio_jumps_are_left_alone():
    # +30% (below the 40% move floor) and -55% (not within 3% of a split ratio)
    raw = [10.0, 13.0, 13.0, 5.85, 6.0]
    adj, events = split_adjust_closes(raw)
    assert events == [] and adj == raw


def test_consecutive_splits_compound():
    raw = [100.0, 100.0, 50.0, 50.0, 5.0, 5.0]  # 2:1 then 10:1
    adj, events = split_adjust_closes(raw)
    assert [(i, b) for i, _, b in events] == [(2, 0.5), (4, 0.1)]
    assert adj == pytest.approx([5.0] * 6)


def test_panel_adjusts_closes_per_symbol_and_keeps_raw_dollar_volume():
    closes = _series(n=6, split_at=3, factor=0.1)
    panel = {
        f"2026-01-{t + 5:02d}": {
            "SPL": {"close": c, "dollar_vol": c * 1000},
            "OK": {"close": 10.0, "dollar_vol": 1e4},
        }
        for t, c in enumerate(closes)
    }
    out = split_adjust_panel(panel)
    days = sorted(out)
    assert out[days[0]]["SPL"]["close"] == pytest.approx(closes[0] * 0.1)
    assert out[days[0]]["SPL"]["dollar_vol"] == panel[days[0]]["SPL"]["dollar_vol"]
    assert all(out[d]["OK"] == panel[d]["OK"] for d in days)
    assert panel[days[0]]["SPL"]["close"] == closes[0]  # input not mutated


def test_split_ratios_match_the_research_simulator():
    # research/tiebreak/core.py is the reference heuristic; src must not drift from it
    tree = ast.parse(_CORE.read_text())
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("SPLIT_K", "SPLIT_R") for t in node.targets
        ):
            exec(compile(ast.Module([node], []), str(_CORE), "exec"), ns)  # noqa: S102
    assert ns["SPLIT_K"] == SPLIT_K
    assert ns["SPLIT_R"] == SPLIT_R
    src = _CORE.read_text()
    assert "(r > 1.4) | (r < 0.6)" in src and "abs(x / best - 1) < 0.03" in src
