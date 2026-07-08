"""Simulated short-accumulation record builder. Pure — no I/O.

Alpaca cannot short our target low-float runners (9/9 probed not shortable,
APIError 42210000), so live paper-shorting is a dead end. Instead we accumulate a
forward, out-of-time dataset: for each short setup from either strategy (fade /
exhaustion) we record the entry-time features, the *would-be* intraday short
outcome (via the live short rules), and the live shortable/easy_to_borrow status
— so the dataset answers both "did it work" and "was it even executable".

This module is the pure record-builder that unifies both strategies' setups into
one `ShortSetupRecord`; the grouped/minute/Alpaca I/O lives in the CLI layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.short_accumulation import ShortSetup, ShortSetupRecord, build_short_record
from src.strategy import ExitParams

_EXITS = ExitParams(
    stop_loss_pct=0.15,
    take_profit_pct=0.10,
    scale_out_fraction=0.0,
    trailing_stop_pct=0.20,
    max_hold_minutes=180,
    exit_spread_pct=1.0,
)

T0 = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)  # 09:30 ET


def _bars(path):
    """Minute bars from prices; o/h/l/c = price, so highs/lows are the price."""
    return [
        {
            "ts": T0 + timedelta(minutes=i),
            "open": p,
            "high": p,
            "low": p,
            "close": p,
            "volume": 1000,
        }
        for i, p in enumerate(path)
    ]


def _setup(strategy="fade", entry_mode="breakout", **kw):
    base = dict(
        symbol="RUNR",
        session_date="2026-07-06",
        strategy=strategy,
        ref_close=15.0,
        trigger_pct=5.0,
        entry_mode=entry_mode,
    )
    base.update(kw)
    return ShortSetup(**base)


def test_breakout_setup_builds_record_with_outcome_and_features():
    # up-break to +7% over 15 (=16.05) triggers, then fades to 13.5 -> short wins
    bars = _bars([15.0, 16.05, 15.0, 14.0, 13.5])
    rec = build_short_record(
        _setup(strategy="fade", entry_mode="breakout", is_fizzle=True),
        bars,
        _EXITS,
        shortable=False,
        easy_to_borrow=False,
    )
    assert isinstance(rec, ShortSetupRecord)
    assert rec.symbol == "RUNR" and rec.strategy == "fade" and rec.entry_mode == "breakout"
    assert rec.triggered is True
    assert abs(rec.entry_price - 16.05) < 1e-9
    assert rec.net_return_pct > 0  # shorted the blow-off, price fell
    assert rec.exit_reason is not None
    # executability captured verbatim
    assert rec.shortable is False and rec.easy_to_borrow is False
    # entry-time features present (no look-ahead)
    assert "vol_accel" in rec.features and "gap_pct" in rec.features
    assert rec.is_fizzle is True


def test_breakdown_setup_enters_on_downside_break():
    # opens green (15.2), breaks down through 14.7 (-2% of 15) then fades
    bars = _bars([15.2, 15.1, 14.6, 14.0, 13.0])
    rec = build_short_record(
        _setup(strategy="exhaustion", entry_mode="breakdown", trigger_pct=2.0, run_gain_pct=120.0),
        bars,
        _EXITS,
        shortable=False,
        easy_to_borrow=False,
    )
    assert rec is not None
    assert rec.strategy == "exhaustion" and rec.entry_mode == "breakdown"
    assert abs(rec.entry_price - 14.6) < 1e-9  # entered on the downside break
    assert rec.net_return_pct > 0
    assert rec.run_gain_pct == 120.0


def test_no_trigger_returns_none():
    # never breaks +5% up over 15 -> breakout finds no entry
    bars = _bars([15.0, 15.2, 15.1, 14.9])
    rec = build_short_record(
        _setup(entry_mode="breakout"), bars, _EXITS, shortable=True, easy_to_borrow=True
    )
    assert rec is None


def test_cost_reduces_recorded_return():
    bars = _bars([15.0, 16.05, 14.0, 13.0])
    gross = build_short_record(_setup(), bars, _EXITS, shortable=False, easy_to_borrow=False)
    net = build_short_record(
        _setup(), bars, _EXITS, shortable=False, easy_to_borrow=False, cost_fn=lambda p: 3.0
    )
    assert abs((gross.net_return_pct - net.net_return_pct) - 3.0) < 1e-9


def test_shortable_status_recorded_even_when_true():
    bars = _bars([15.0, 16.05, 14.0, 13.0])
    rec = build_short_record(_setup(), bars, _EXITS, shortable=True, easy_to_borrow=True)
    assert rec.shortable is True and rec.easy_to_borrow is True
