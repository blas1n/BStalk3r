"""Strategy: entry/exit decision rules. Pure, deterministic."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.models import PositionState
from src.strategy import EntryParams, ExitParams, evaluate_entry, evaluate_exit

from tests.conftest import make_snapshot

ENTRY = EntryParams(
    min_price=1.0,
    max_price=50.0,
    min_day_change_pct=5.0,
    max_day_change_pct=40.0,
    min_rvol=8.0,
    min_volume_acceleration=3.0,
    max_spread_pct=1.0,
)

EXIT = ExitParams(
    stop_loss_pct=0.05,
    take_profit_pct=0.15,
    scale_out_fraction=0.5,
    trailing_stop_pct=0.08,
    max_hold_minutes=30,
    exit_spread_pct=1.5,
)

T0 = datetime(2026, 6, 10, 14, 0, 0)


def _pos(**overrides) -> PositionState:
    defaults = dict(
        symbol="RUNR",
        entry_price=10.0,
        qty=100,
        entry_time=T0,
        current_price=10.0,
        peak_price=10.0,
        current_spread_pct=0.3,
        scaled_out=False,
    )
    defaults.update(overrides)
    return PositionState(**defaults)


# ---- entry ----------------------------------------------------------------


def test_enter_when_all_conditions_met():
    d = evaluate_entry(make_snapshot(), ENTRY, holding=False)
    assert d.enter is True
    assert all(d.reasons.values())


def test_no_entry_when_already_holding():
    d = evaluate_entry(make_snapshot(), ENTRY, holding=True)
    assert d.enter is False
    assert d.reasons["not_holding"] is False


def test_no_entry_when_one_rule_fails():
    d = evaluate_entry(make_snapshot(rvol=2.0), ENTRY, holding=False)
    assert d.enter is False
    assert d.reasons["rvol"] is False


def test_entry_score_present():
    d = evaluate_entry(make_snapshot(), ENTRY, holding=False)
    assert d.score > 0


# ---- exit -----------------------------------------------------------------


def test_stop_loss_triggers_full_exit():
    d = evaluate_exit(_pos(current_price=9.40), EXIT, now=T0 + timedelta(minutes=5))
    assert d.should_exit is True
    assert d.reason == "stop_loss"
    assert d.fraction == 1.0


def test_take_profit_scales_out_half_first():
    d = evaluate_exit(
        _pos(current_price=11.6, peak_price=11.6), EXIT, now=T0 + timedelta(minutes=5)
    )
    assert d.should_exit is True
    assert d.reason == "take_profit_scale"
    assert d.fraction == 0.5


def test_take_profit_not_repeated_after_scale_out():
    d = evaluate_exit(
        _pos(current_price=11.6, peak_price=11.6, scaled_out=True),
        EXIT,
        now=T0 + timedelta(minutes=5),
    )
    assert d.should_exit is False


def test_trailing_stop_from_peak():
    # peaked at 12.0, now 10.9 -> -9.2% from peak, beyond 8% trail
    d = evaluate_exit(
        _pos(current_price=10.9, peak_price=12.0), EXIT, now=T0 + timedelta(minutes=5)
    )
    assert d.should_exit is True
    assert d.reason == "trailing_stop"
    assert d.fraction == 1.0


def test_max_hold_forces_exit():
    d = evaluate_exit(_pos(current_price=10.2), EXIT, now=T0 + timedelta(minutes=31))
    assert d.should_exit is True
    assert d.reason == "max_hold"


def test_spread_blowout_exits():
    d = evaluate_exit(
        _pos(current_price=10.2, current_spread_pct=2.0), EXIT, now=T0 + timedelta(minutes=5)
    )
    assert d.should_exit is True
    assert d.reason == "spread_widened"


def test_force_close_overrides_everything():
    d = evaluate_exit(
        _pos(current_price=10.2), EXIT, now=T0 + timedelta(minutes=1), force_close=True
    )
    assert d.should_exit is True
    assert d.reason == "force_close"
    assert d.fraction == 1.0


def test_stop_loss_takes_priority_over_profit():
    # contrived: deep loss must win even if other flags would not fire
    d = evaluate_exit(_pos(current_price=9.0, peak_price=12.0), EXIT, now=T0 + timedelta(minutes=2))
    assert d.reason == "stop_loss"


def test_quiet_winner_holds():
    d = evaluate_exit(
        _pos(current_price=10.5, peak_price=10.6), EXIT, now=T0 + timedelta(minutes=5)
    )
    assert d.should_exit is False
