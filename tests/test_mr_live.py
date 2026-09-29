"""Live RSI-2 rebalance decisions. Pure — no I/O.

Runs once near the close: given each liquid name's recent daily closes + today's
provisional close, plus the current open positions, decide what to SELL (RSI
bounced back / max-hold) and what to BUY (oversold dip in an uptrend, capacity-
capped, most-oversold first). This is the live brain; the CLI feeds it real data
and routes decisions through the (dry-run) execution engine.
"""

from __future__ import annotations

from src.mr_live import mr_decisions, trailing_avg_dollar_vol


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


# --- R5 tie-break: equal RSI -> larger 20-day avg dollar volume first -------------
# Simple-average RSI-2 is exactly 0 after two down days, so hundreds of names tie
# at RSI=0 every day. Without a secondary key the pick among ties followed Python
# set order (random per run). R5 (pre-registered, docs/preregistration/
# 2026-09-tiebreak-r5.md): RSI asc, then 20d avg $vol (D-1..D-20) desc, then symbol.

_TIE_CLOSES = [20.0, 18.0]  # with current 16.0 -> two losses -> RSI-2 == 0
_TIE_CUR = 16.0


def _tie_inputs(syms):
    closes = {s: list(_TIE_CLOSES) for s in syms}
    cur = {s: _TIE_CUR for s in syms}
    dvol = {s: 1e8 for s in syms}  # last-day $vol: liquidity filter only
    return closes, cur, dvol


def test_ties_at_equal_rsi_ordered_by_avg_dollar_vol_desc():
    closes, cur, dvol = _tie_inputs(["AAA", "BBB", "CCC"])
    adv = {"AAA": 1e7, "BBB": 9e7, "CCC": 5e7}
    d = mr_decisions(
        closes,
        cur,
        dvol,
        held={},
        rank_dollar_vol=adv,
        **_params(ma_period=0, max_positions=2),
    )
    assert d["entries"] == ["BBB", "CCC"]


def test_tie_break_is_deterministic_regardless_of_insertion_order():
    syms = ["MMM", "AAA", "ZZZ", "KKK", "BBB"]
    adv = {"MMM": 5e7, "AAA": 5e7, "ZZZ": 9e7, "KKK": 5e7, "BBB": 1e7}
    c1, p1, v1 = _tie_inputs(syms)
    c2, p2, v2 = _tie_inputs(list(reversed(syms)))
    kw = _params(ma_period=0, max_positions=3)
    d1 = mr_decisions(c1, p1, v1, held={}, rank_dollar_vol=adv, **kw)
    d2 = mr_decisions(c2, p2, v2, held={}, rank_dollar_vol=dict(reversed(list(adv.items()))), **kw)
    assert d1["entries"] == d2["entries"] == ["ZZZ", "AAA", "KKK"]  # adv desc, then symbol


def test_rsi_still_dominates_avg_dollar_vol():
    closes = {"LOW": [20.0, 18.0], "HIGH": [20.0, 18.0]}
    cur = {"LOW": 16.0, "HIGH": 18.3}  # LOW: RSI 0; HIGH: small bounce -> higher RSI
    adv = {"LOW": 1e6, "HIGH": 1e10}
    d = mr_decisions(
        closes,
        cur,
        {"LOW": 1e8, "HIGH": 1e8},
        held={},
        rank_dollar_vol=adv,
        **_params(ma_period=0, entry_rsi=40, max_positions=1),
    )
    assert d["entries"] == ["LOW"]


def test_trailing_avg_dollar_vol_uses_d1_to_d20_and_excludes_today():
    # 22 prior sessions + today. D-21 and today carry huge spikes that must be ignored.
    dates = [f"2026-01-{i:02d}" for i in range(1, 24)]  # 23 dates; last = today
    today = dates[-1]
    panel = {d: {"AAA": {"close": 10.0, "dollar_vol": float(i)}} for i, d in enumerate(dates)}
    panel[dates[1]]["AAA"]["dollar_vol"] = 1e12  # D-21: outside the window
    panel[today]["AAA"]["dollar_vol"] = 1e12  # today: excluded
    got = trailing_avg_dollar_vol(panel, today, window=20)
    # D-1..D-20 are indices 21..2 -> mean(2..21) = 11.5
    assert got["AAA"] == 11.5


def test_trailing_avg_dollar_vol_missing_day_counts_as_zero_like_simulator():
    # research/tiebreak ADV = sum(nan_to_num(DV[t-20..t-1])) / 20: a session the
    # symbol didn't trade contributes 0 to the numerator; the divisor stays 20.
    dates = [f"2026-02-{i:02d}" for i in range(1, 22)]  # 20 prior + today
    panel = {d: {"AAA": {"close": 10.0, "dollar_vol": 100.0}} for d in dates}
    panel[dates[5]] = {}  # AAA absent one session in the window
    got = trailing_avg_dollar_vol(panel, dates[-1], window=20)
    assert got["AAA"] == 100.0 * 19 / 20


def test_trailing_avg_dollar_vol_short_history_averages_what_exists():
    panel = {
        "2026-03-02": {"AAA": {"close": 1.0, "dollar_vol": 10.0}},
        "2026-03-03": {"AAA": {"close": 1.0, "dollar_vol": 30.0}},
        "2026-03-04": {"AAA": {"close": 1.0, "dollar_vol": 999.0}},  # today
    }
    assert trailing_avg_dollar_vol(panel, "2026-03-04", window=20) == {"AAA": 20.0}


def test_missing_rank_dollar_vol_sorts_last_among_ties():
    closes, cur, dvol = _tie_inputs(["AAA", "BBB"])
    d = mr_decisions(
        closes,
        cur,
        dvol,
        held={},
        rank_dollar_vol={"BBB": 1.0},
        **_params(ma_period=0, max_positions=1),
    )
    assert d["entries"] == ["BBB"]  # AAA has no ADV -> ranked after any known value
