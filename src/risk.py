"""Risk gate + position sizing. Pure, deterministic.

The last line of defense before any order: even if strategy says "go", these
hard limits can veto it. No order is placed unless `check_entry_allowed`
returns allowed=True.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.models import RiskGate, RiskState


@dataclass(frozen=True)
class RiskParams:
    max_risk_per_trade_pct: float
    max_position_value: float
    max_concurrent_positions: int
    daily_max_loss_pct: float
    max_daily_trades: int
    stop_loss_pct: float


def check_entry_allowed(state: RiskState, p: RiskParams) -> RiskGate:
    """Veto gate for new entries. Allowed only if every guard passes."""
    loss_floor = -abs(p.daily_max_loss_pct) * state.account_equity
    reasons = {
        "data_healthy": state.data_healthy,
        "below_max_positions": state.open_positions < p.max_concurrent_positions,
        "below_max_daily_trades": state.trades_today < p.max_daily_trades,
        "daily_loss_ok": state.realized_pnl_today > loss_floor,
    }
    return RiskGate(allowed=all(reasons.values()), reasons=reasons)


def position_size(equity: float, price: float, p: RiskParams) -> int:
    """Shares to buy.

    Bounded by (a) risk budget: at most `max_risk_per_trade_pct` of equity is
    lost if the stop fires, and (b) `max_position_value` notional. Returns the
    smaller, never negative.
    """
    if price <= 0 or equity <= 0:
        return 0

    stop_distance = price * p.stop_loss_pct
    if stop_distance <= 0:
        return 0

    risk_budget = equity * p.max_risk_per_trade_pct
    qty_by_risk = math.floor(risk_budget / stop_distance)
    qty_by_value = math.floor(p.max_position_value / price)
    return max(min(qty_by_risk, qty_by_value), 0)
