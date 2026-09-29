"""Backtests band on the as-traded close, return on the adjusted close (#55).

Split-adjusting closes (#47) restates history in today's units. A later 1:10
reverse split lifts a name that traded at $2 to a back-adjusted $20, inside the
$5-1000 band, so the band filter would admit a stock nobody could have screened
in at the time. `adjust_panel` records the as-traded close next to the adjusted
one; each engine bands on it and still computes returns on the adjusted close."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from src.calendar_strat import equal_weight_returns
from src.mean_reversion import mean_reversion_trades
from src.sentiment import sentiment_backtest
from src.splits import SplitEvent, adjust_panel
from src.xsectional import cross_sectional_backtest, traded_close


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_DATES = _weekdays(date(2026, 1, 5), 20)
_EXEC = 10  # execution-date index of every split below


def _panel(closes_by_sym: dict[str, list[float]]) -> dict:
    return {
        d: {s: {"close": cs[i], "dollar_vol": cs[i] * 1e6} for s, cs in closes_by_sym.items()}
        for i, d in enumerate(_DATES)
    }


def _traded(panel, sym):
    return [traded_close(panel[d][sym]) for d in _DATES]


def _adj(panel, sym):
    return [panel[d][sym]["close"] for d in _DATES]


_REV = SplitEvent("REV", _DATES[_EXEC], 10, 1)  # 1-for-10 reverse split, factor 10


# --- adjust_panel records the as-traded close -----------------------------------


def test_reverse_split_in_raw_cache_keeps_the_2_dollar_as_traded_close():
    raw = [2.0 + 0.01 * i for i in range(_EXEC)] + [20.0 + 0.1 * i for i in range(10)]
    out, applied = adjust_panel(_panel({"REV": raw}), [_REV])
    assert applied == 1
    assert _adj(out, "REV")[:_EXEC] == pytest.approx([c * 10 for c in raw[:_EXEC]])
    assert _traded(out, "REV") == pytest.approx(raw)  # $2 then $20: what printed


def test_reverse_split_already_reflected_in_the_cache_is_undone_for_the_band():
    """Most cache rows were backfilled after their split (already restated):
    no jump to locate, but the $20 history still traded at $2."""
    cached = [20.0 + 0.1 * i for i in range(20)]
    out, applied = adjust_panel(_panel({"REV": cached}), [_REV])
    assert applied == 0
    assert _adj(out, "REV") == pytest.approx(cached)  # returns untouched
    assert _traded(out, "REV") == pytest.approx([c / 10 for c in cached[:_EXEC]] + cached[_EXEC:])


def test_as_of_boundary_before_execution_date_still_trades_pre_split():
    """Jump one session early (MUU-style): that session's cached row is already
    in post-split units, but it traded before the split."""
    raw = [2.0] * (_EXEC - 1) + [20.0] * (20 - _EXEC + 1)
    out, applied = adjust_panel(_panel({"REV": raw}), [_REV])
    assert applied == 1
    assert _traded(out, "REV") == pytest.approx([2.0] * _EXEC + [20.0] * (20 - _EXEC))


def test_forward_split_keeps_the_above_band_as_traded_close():
    fwd = SplitEvent("FWD", _DATES[_EXEC], 1, 10)  # 10-for-1, $1500 -> $150
    raw = [1500.0] * _EXEC + [150.0] * 10
    out, _ = adjust_panel(_panel({"FWD": raw}), [fwd])
    assert _adj(out, "FWD") == pytest.approx([150.0] * 20)
    assert _traded(out, "FWD") == pytest.approx(raw)


def test_event_after_the_last_cached_session_that_the_cache_never_saw():
    """Split executes after the window and the cache shows no jump: those rows
    were fetched before it, so they are already as-traded."""
    late = SplitEvent("REV", (date(2026, 2, 6)).isoformat(), 10, 1)
    raw = [2.0] * 20
    out, applied = adjust_panel(_panel({"REV": raw}), [late])
    assert applied == 0
    assert _traded(out, "REV") == pytest.approx(raw)


def test_event_in_a_gap_of_the_symbols_history_says_nothing_about_earlier_rows():
    """Ticker reuse (PARA: Paramount rows end 2025-08-06, a new PARA appears
    2026-08-07; its 1:20 split executes 2026-05-08 in between). No rows around
    the execution date means no evidence the earlier rows were restated."""
    old = _DATES[:6]
    new = _weekdays(date(2026, 6, 1), 5)
    panel = {d: {"PARA": {"close": 12.57, "dollar_vol": 1e8}} for d in old}
    panel.update({d: {"PARA": {"close": 1.7, "dollar_vol": 1e5}} for d in new})
    ev = SplitEvent("PARA", "2026-05-08", 20, 1)
    out, applied = adjust_panel(panel, [ev])
    assert applied == 0
    assert [traded_close(out[d]["PARA"]) for d in sorted(out)] == [12.57] * 6 + [1.7] * 5


def test_symbol_without_events_trades_at_its_close():
    raw = [8.0 + i for i in range(20)]
    out, _ = adjust_panel(_panel({"AAA": raw}), [_REV])
    assert _traded(out, "AAA") == raw
    assert _traded(_panel({"AAA": raw}), "AAA") == raw  # unadjusted panel too


def test_dollar_volume_is_untouched_by_the_as_traded_record():
    raw = [2.0] * _EXEC + [20.0] * 10
    panel = _panel({"REV": raw})
    out, _ = adjust_panel(panel, [_REV])
    assert [out[d]["REV"]["dollar_vol"] for d in _DATES] == [
        panel[d]["REV"]["dollar_vol"] for d in _DATES
    ]


# --- engines band on the as-traded close ----------------------------------------


def _mr_series(n: int = 60) -> tuple[list[str], list[float], list[float]]:
    dates = _weekdays(date(2026, 1, 5), n)
    closes = [
        20.0 + 0.2 * t + (1.5 if t % 4 == 1 else 0.0) - (1.5 if t % 7 == 3 else 0) for t in range(n)
    ]
    return dates, closes, [1e7] * n


_MR = dict(entry_rsi=30.0, exit_rsi=60.0, ma_period=5, max_hold=3, min_price=5.0, max_price=1000.0)


def test_mean_reversion_skips_entries_whose_as_traded_close_is_below_the_band():
    dates, closes, dvols = _mr_series()
    assert mean_reversion_trades(dates, closes, dvols, **_MR)  # adjusted $20: would trade
    assert (
        mean_reversion_trades(dates, closes, dvols, traded_closes=[c / 10 for c in closes], **_MR)
        == []
    )


def test_mean_reversion_above_band_on_both_bases_is_unaffected_and_returns_are_adjusted():
    dates, closes, dvols = _mr_series()
    base = mean_reversion_trades(dates, closes, dvols, **_MR)
    got = mean_reversion_trades(dates, closes, dvols, traded_closes=[c / 2 for c in closes], **_MR)
    assert got == base  # $10 as traded, $20 adjusted: same trades, adjusted returns


def _panel_with(extra: dict[str, list[tuple[float, float]]], n: int = 30) -> dict:
    """Ordinary names plus `extra` = {sym: [(adjusted, traded), ...]}."""
    dates = _weekdays(date(2026, 1, 5), n)
    out = {}
    for t, d in enumerate(dates):
        day = {
            f"S{k}": {"close": 20.0 + k + ((t * (k + 3)) % 7) * 0.4, "dollar_vol": 1e8}
            for k in range(8)
        }
        for sym, series in extra.items():
            adj, tr = series[t]
            rec = {"close": adj, "dollar_vol": 1e8}
            if tr != adj:
                rec["traded_close"] = tr
            day[sym] = rec
        out[d] = day
    return out


def _wild(scale: float, n: int = 30) -> list[float]:
    return [scale * (1.0 + (0.3 if t % 2 else -0.2)) for t in range(n)]


def _drop(panel: dict, sym: str) -> dict:
    return {d: {s: r for s, r in day.items() if s != sym} for d, day in panel.items()}


_ENGINE_RUNS = {
    "cross_sectional_backtest": lambda p: cross_sectional_backtest(
        p, "reversal", 1, 2, 0.2, 5.0, 1000.0, 1.0
    ),
    "equal_weight_returns": lambda p: equal_weight_returns(p, 5.0, 1000.0, 1.0),
    "sentiment_backtest": lambda p: sentiment_backtest(
        p,
        {(s, d): {"mean": (sum(map(ord, s)) % 7 - 3) / 3, "n": 3} for d in p for s in p[d]},
        1,
        2,
        0.2,
        5.0,
        1000.0,
        1.0,
        1,
    ),
}


@pytest.mark.parametrize("engine", list(_ENGINE_RUNS))
def test_panel_engine_excludes_a_name_that_traded_below_the_band(engine):
    run = _ENGINE_RUNS[engine]
    adj = _wild(20.0)
    below = _panel_with({"REV": [(a, a / 10) for a in adj]})
    assert run(below) == run(_drop(below, "REV"))  # $2 as traded: not in the universe
    as_if_traded = _panel_with({"REV": [(a, a) for a in adj]})
    assert run(as_if_traded) != run(_drop(as_if_traded, "REV"))  # control: REV matters


@pytest.mark.parametrize("engine", list(_ENGINE_RUNS))
def test_panel_engine_above_band_on_both_bases_is_unaffected(engine):
    run = _ENGINE_RUNS[engine]
    adj = _wild(20.0)
    both = _panel_with({"ABV": [(a, a / 2) for a in adj]})  # $10 traded, $20 adjusted
    assert run(both) == run(_panel_with({"ABV": [(a, a) for a in adj]}))
