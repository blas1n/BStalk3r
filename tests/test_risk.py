"""Risk gate + position sizing. Pure, deterministic."""

from __future__ import annotations

from src.models import RiskState
from src.risk import RiskParams, check_entry_allowed, position_size

PARAMS = RiskParams(
    max_risk_per_trade_pct=0.01,
    max_position_value=2000.0,
    max_concurrent_positions=2,
    daily_max_loss_pct=0.03,
    max_daily_trades=20,
    stop_loss_pct=0.05,
)


def _state(**overrides) -> RiskState:
    defaults = dict(
        account_equity=100_000.0,
        open_positions=0,
        trades_today=0,
        realized_pnl_today=0.0,
        data_healthy=True,
    )
    defaults.update(overrides)
    return RiskState(**defaults)


def test_healthy_state_allows_entry():
    gate = check_entry_allowed(_state(), PARAMS)
    assert gate.allowed is True
    assert all(gate.reasons.values())


def test_max_concurrent_positions_blocks():
    gate = check_entry_allowed(_state(open_positions=2), PARAMS)
    assert gate.allowed is False
    assert gate.reasons["below_max_positions"] is False


def test_daily_trade_cap_blocks():
    gate = check_entry_allowed(_state(trades_today=20), PARAMS)
    assert gate.allowed is False
    assert gate.reasons["below_max_daily_trades"] is False


def test_daily_loss_limit_blocks():
    # -3% of 100k = -3000; at -3100 we must halt
    gate = check_entry_allowed(_state(realized_pnl_today=-3100.0), PARAMS)
    assert gate.allowed is False
    assert gate.reasons["daily_loss_ok"] is False


def test_small_loss_still_allows():
    gate = check_entry_allowed(_state(realized_pnl_today=-500.0), PARAMS)
    assert gate.allowed is True


def test_unhealthy_data_blocks_entry():
    gate = check_entry_allowed(_state(data_healthy=False), PARAMS)
    assert gate.allowed is False
    assert gate.reasons["data_healthy"] is False


# ---- position sizing ------------------------------------------------------


def test_position_size_capped_by_risk_budget():
    # risk budget = 100k * 1% = 1000; stop distance = 10 * 5% = 0.5 -> 2000 shares
    # but max_position_value 2000 / 10 = 200 shares is the binding cap.
    qty = position_size(100_000.0, 10.0, PARAMS)
    assert qty == 200


def test_position_size_capped_by_risk_when_value_room_is_large():
    big = RiskParams(
        max_risk_per_trade_pct=0.01,
        max_position_value=1_000_000.0,
        max_concurrent_positions=2,
        daily_max_loss_pct=0.03,
        max_daily_trades=20,
        stop_loss_pct=0.05,
    )
    # risk budget 1000 / (10*0.05=0.5) = 2000 shares, value cap not binding
    qty = position_size(100_000.0, 10.0, big)
    assert qty == 2000


def test_position_size_never_negative():
    assert position_size(0.0, 10.0, PARAMS) == 0


def test_position_size_zero_price_safe():
    assert position_size(100_000.0, 0.0, PARAMS) == 0
