"""H-B v2 — realistic intraday-trigger exhaustion short. Pure, no I/O.

v1 (`exhaustion.py`) had two biases baked in: it only shorted days that *closed*
red (EOD selection — you can't know that at the open) and modeled the fill at the
red-day open (look-ahead). v2 fixes both: the short candidate is simply the
session AFTER a qualifying parabolic run — regardless of how it closes — and the
entry is reconstructed from that day's minute bars (first intraday up-break of the
run-end close), then exited on the live short rules via `simulate_short_trade`.
Green-reversal days that push up and fade are therefore INCLUDED, and days that
never trigger produce no trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.exhaustion_intraday import (
    IntradayExhaustionShort,
    qualifying_run_ends,
    simulate_run_end_short,
)
from src.strategy import ExitParams


def _d(date, o, h, low, c):
    return {"date": date, "open": o, "high": h, "low": low, "close": c}


def _bars(prev_close, path, start="2026-07-06T13:30:00+00:00"):
    """Minute bars from a list of prices; each bar's o/h/l/c = the price."""
    t0 = datetime.fromisoformat(start)
    out = []
    for i, p in enumerate(path):
        ts = t0 + timedelta(minutes=i)
        out.append({"ts": ts, "open": p, "high": p, "low": p, "close": p, "volume": 1000})
    return out


_EXITS = ExitParams(
    stop_loss_pct=0.15,
    take_profit_pct=0.10,
    scale_out_fraction=0.0,
    trailing_stop_pct=0.20,
    max_hold_minutes=180,
    exit_spread_pct=1.0,
)


def test_qualifying_run_ends_flags_next_session_regardless_of_color():
    # RUNR ran 10 -> 12 -> 15 (+50% over 2d). The candidate short day is the
    # NEXT session (2026-07-06) — no red-close requirement.
    seq = {
        "RUNR": [
            _d("2026-07-01", 10, 10, 10, 10.0),
            _d("2026-07-02", 12, 12, 12, 12.0),
            _d("2026-07-03", 15, 15, 15, 15.0),
            _d("2026-07-06", 15.0, 18.0, 14.0, 17.0),  # GREEN next day — still a candidate
        ]
    }
    ends = qualifying_run_ends(seq, run_days=2, run_min_gain_pct=40.0, min_price=1, max_price=50)
    assert len(ends) == 1
    e = ends[0]
    assert e.symbol == "RUNR"
    assert e.run_end_date == "2026-07-03"
    assert e.short_day_date == "2026-07-06"
    assert abs(e.prev_close - 15.0) < 1e-9  # run-end close = intraday break reference
    assert abs(e.run_gain_pct - 50.0) < 1e-9


def test_green_reversal_day_that_fades_is_shorted():
    # Run ended at close 15. Next day pushes up through 15 (+~7% break triggers the
    # fade short) then rolls over and fades hard — a GREEN-open blow-off that dies.
    ends = qualifying_run_ends(
        {
            "R": [
                _d("2026-07-02", 10, 10, 10, 10.0),
                _d("2026-07-03", 15, 15, 15, 15.0),
                _d("2026-07-06", 15, 16, 12, 13.0),
            ]
        },
        run_days=1,
        run_min_gain_pct=40.0,
        min_price=1,
        max_price=50,
    )
    assert len(ends) == 1
    # up-break to 16.05 (+7% over prev_close 15) triggers short, then fades to 13.5
    bars = _bars(15.0, [15.0, 16.05, 15.0, 14.0, 13.5])
    res = simulate_run_end_short(
        ends[0], bars, entry_min_change=5.0, min_price=1, max_price=50, exit_params=_EXITS
    )
    assert isinstance(res, IntradayExhaustionShort)
    assert res.trade.entered is True
    assert res.trade.net_return_pct > 0  # shorted the blow-off, price fell


def test_no_trigger_day_returns_none():
    ends = qualifying_run_ends(
        {
            "R": [
                _d("2026-07-02", 10, 10, 10, 10.0),
                _d("2026-07-03", 15, 15, 15, 15.0),
                _d("2026-07-06", 14.5, 14.5, 12, 12.5),
            ]
        },
        run_days=1,
        run_min_gain_pct=40.0,
        min_price=1,
        max_price=50,
    )
    # Day never breaks up through prev_close (15) by +5% — no entry.
    bars = _bars(15.0, [14.5, 14.0, 13.0, 12.0])
    res = simulate_run_end_short(
        ends[0], bars, entry_min_change=5.0, min_price=1, max_price=50, exit_params=_EXITS
    )
    assert res is None


def test_no_setup_if_run_too_small():
    ends = qualifying_run_ends(
        {
            "X": [
                _d("2026-07-03", 10, 10, 10, 10.0),
                _d("2026-07-04", 10.5, 10.5, 10.5, 10.5),  # +5% only
                _d("2026-07-06", 10.5, 11, 10, 10.2),
            ]
        },
        run_days=1,
        run_min_gain_pct=40.0,
        min_price=1,
        max_price=50,
    )
    assert ends == []


def test_band_filter_on_run_end_price():
    ends = qualifying_run_ends(
        {
            "PRICEY": [
                _d("2026-07-03", 100, 100, 100, 100.0),
                _d("2026-07-04", 160, 160, 160, 160.0),  # ran but run-end > $50
                _d("2026-07-06", 160, 170, 150, 155.0),
            ]
        },
        run_days=1,
        run_min_gain_pct=40.0,
        min_price=1,
        max_price=50,
    )
    assert ends == []


def test_cost_reduces_short_return():
    ends = qualifying_run_ends(
        {
            "R": [
                _d("2026-07-02", 10, 10, 10, 10.0),
                _d("2026-07-03", 15, 15, 15, 15.0),
                _d("2026-07-06", 15, 16, 12, 13.0),
            ]
        },
        run_days=1,
        run_min_gain_pct=40.0,
        min_price=1,
        max_price=50,
    )
    path = [15.0, 16.05, 14.0, 13.0]
    gross = simulate_run_end_short(
        ends[0],
        _bars(15.0, path),
        entry_min_change=5.0,
        min_price=1,
        max_price=50,
        exit_params=_EXITS,
    )
    net = simulate_run_end_short(
        ends[0],
        _bars(15.0, path),
        entry_min_change=5.0,
        min_price=1,
        max_price=50,
        exit_params=_EXITS,
        cost_fn=lambda price: 3.0,
    )
    assert gross.trade.net_return_pct - net.trade.net_return_pct == 3.0
