"""Internal data structures shared across the rule engine.

Plain dataclasses (and one str Enum) so the pure logic modules — scanner,
strategy, risk — never depend on Alpaca SDK types and stay trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class MarketSnapshot:
    """A point-in-time view of one symbol used by scanner + strategy.

    `float_shares` / `market_cap` are nullable in v0: no free data source
    provides them reliably, and the rules must work without them.
    """

    symbol: str
    last_price: float
    day_change_pct: float
    rvol: float
    volume_acceleration: float
    spread_pct: float
    bid_price: float | None = None
    ask_price: float | None = None
    float_shares: float | None = None
    market_cap: float | None = None
    timestamp: datetime | None = None


@dataclass(frozen=True)
class PositionState:
    """Live state of one open position, fed to the exit evaluator."""

    symbol: str
    entry_price: float
    qty: int
    entry_time: datetime
    current_price: float
    peak_price: float
    current_spread_pct: float = 0.0
    scaled_out: bool = False


@dataclass(frozen=True)
class EntryDecision:
    enter: bool
    score: float
    reasons: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str | None = None
    fraction: float = 0.0  # portion of qty to sell (1.0 = full exit)


@dataclass(frozen=True)
class RiskState:
    account_equity: float
    open_positions: int
    trades_today: int
    realized_pnl_today: float
    data_healthy: bool = True


@dataclass(frozen=True)
class RiskGate:
    allowed: bool
    reasons: dict[str, bool] = field(default_factory=dict)
