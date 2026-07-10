"""Live RSI-2 rebalance decisions. Pure — no I/O.

Runs once near the close: given each liquid name's recent daily closes + today's
provisional close, plus the current open positions, decide what to SELL (RSI
bounced back / max-hold) and what to BUY (oversold dip in an uptrend, capacity-
capped, most-oversold first). This is the live brain; the CLI feeds it real data
and routes decisions through the (dry-run) execution engine.
"""

from __future__ import annotations

from src.mr_live import mr_decisions


def _params(**kw):
    base = dict(
        rsi_period=2,
        entry_rsi=15.0,
        exit_rsi=70.0,
        ma_period=3,
        max_hold=5,
        max_positions=10,
        min_price=1.0,
        max_price=1000.0,
        min_dollar_vol=0.0,
    )
    base.update(kw)
    return base


def test_exit_when_rsi_bounces_back():
    # held AAA; recent closes rose then today pops -> RSI-2 high -> exit
    closes = {"AAA": [10.0, 9.0, 8.0]}  # falling
    cur = {"AAA": 12.0}  # big pop today -> RSI-2 jumps above exit
    d = mr_decisions(closes, cur, {"AAA": 1e8}, held={"AAA": 2}, **_params())
    assert "AAA" in d["exits"]


def test_exit_on_max_hold():
    closes = {"AAA": [10.0, 10.0, 10.0]}
    cur = {"AAA": 10.0}  # RSI neutral, not a bounce
    d = mr_decisions(closes, cur, {"AAA": 1e8}, held={"AAA": 5}, **_params(max_hold=5))
    assert "AAA" in d["exits"]  # held >= max_hold


def test_entry_on_oversold_dip():
    # BBB fell 2 days -> RSI-2 = 0 (oversold); regime off -> entry
    closes = {"BBB": [20.0, 18.0]}
    cur = {"BBB": 16.0}
    d = mr_decisions(closes, cur, {"BBB": 1e8}, held={}, **_params(ma_period=0))
    assert "BBB" in d["entries"]


def test_regime_filter_blocks_dip_below_ma():
    # oversold but below the SMA (downtrend) -> blocked; a parallel one above -> in
    closes = {"DOWN": [20.0, 19.0, 18.0, 17.0], "UP": [10.0, 12.0, 14.0, 16.0]}
    cur = {"DOWN": 15.0, "UP": 15.5}  # DOWN below SMA3, UP above SMA3
    d = mr_decisions(closes, cur, {"DOWN": 1e8, "UP": 1e8}, held={}, **_params(ma_period=3))
    assert "DOWN" not in d["entries"]  # regime (below SMA) blocks the falling knife


def test_entry_capacity_and_ranking():
    # distinct RSI-2 (all <= entry), only 1 free slot -> take the MOST oversold (B)
    closes = {"A": [20.0, 18.0], "B": [20.0, 18.0], "C": [20.0, 18.0]}
    cur = {"A": 18.3, "B": 16.0, "C": 18.6}  # B: 2 losses -> RSI 0; A/C: small bounce -> higher
    d = mr_decisions(
        closes,
        cur,
        {"A": 1e8, "B": 1e8, "C": 1e8},
        held={},
        **_params(ma_period=0, entry_rsi=40, max_positions=1),
    )
    assert d["entries"] == ["B"]  # lowest RSI wins the single slot


def test_no_entry_when_book_full():
    closes = {"NEW": [20.0, 15.0]}
    cur = {"NEW": 12.0}
    d = mr_decisions(
        closes,
        cur,
        {"NEW": 1e8},
        held={"X": 1, "Y": 2},
        **_params(ma_period=0, entry_rsi=100, max_positions=2),
    )
    assert d["entries"] == []  # 2 held, book of 2 -> no room


def test_filters_illiquid_and_out_of_band_and_held():
    closes = {"PEN": [1.0, 0.5], "HELD": [20.0, 15.0], "ILQ": [20.0, 15.0]}
    cur = {"PEN": 0.4, "HELD": 12.0, "ILQ": 12.0}
    dvol = {"PEN": 1e8, "HELD": 1e8, "ILQ": 100.0}  # ILQ illiquid
    d = mr_decisions(
        closes,
        cur,
        dvol,
        held={"HELD": 1},
        **_params(ma_period=0, entry_rsi=100, min_price=1.0, min_dollar_vol=1e6),
    )
    assert "PEN" not in d["entries"]  # below min_price
    assert "ILQ" not in d["entries"]  # illiquid
    assert "HELD" not in d["entries"]  # already held
