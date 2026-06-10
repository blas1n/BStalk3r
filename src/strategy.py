"""Entry / exit decision rules. Pure, deterministic, no I/O, no LLM.

The realtime loop calls these on every poll. Keeping them side-effect free is
what lets the system stay fast and fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models import EntryDecision, ExitDecision, MarketSnapshot, PositionState
from src.scanner import score_candidate


@dataclass(frozen=True)
class EntryParams:
    min_price: float
    max_price: float
    min_day_change_pct: float
    max_day_change_pct: float
    min_rvol: float
    min_volume_acceleration: float
    max_spread_pct: float


@dataclass(frozen=True)
class ExitParams:
    stop_loss_pct: float
    take_profit_pct: float
    scale_out_fraction: float
    trailing_stop_pct: float
    max_hold_minutes: float
    exit_spread_pct: float


def evaluate_entry(s: MarketSnapshot, p: EntryParams, holding: bool) -> EntryDecision:
    """Long-entry gate. Enter only if *every* reason is True."""
    reasons = {
        "not_holding": not holding,
        "price_floor": s.last_price >= p.min_price,
        "price_cap": s.last_price <= p.max_price,
        "day_change_floor": s.day_change_pct >= p.min_day_change_pct,
        "day_change_cap": s.day_change_pct <= p.max_day_change_pct,
        "rvol": s.rvol >= p.min_rvol,
        "volume_accel": s.volume_acceleration >= p.min_volume_acceleration,
        "spread_ok": s.spread_pct <= p.max_spread_pct,
    }
    return EntryDecision(enter=all(reasons.values()), score=score_candidate(s), reasons=reasons)


def evaluate_exit(
    pos: PositionState,
    p: ExitParams,
    now: datetime,
    force_close: bool = False,
) -> ExitDecision:
    """Exit gate, checked in priority order (protective rules win).

    Returns a full exit (fraction 1.0) or a partial scale-out (the first
    take-profit), or no exit.
    """
    if force_close:
        return ExitDecision(True, "force_close", 1.0)

    gain = (pos.current_price - pos.entry_price) / pos.entry_price

    # Protective rules first — capital preservation beats profit taking.
    if gain <= -p.stop_loss_pct:
        return ExitDecision(True, "stop_loss", 1.0)

    held_min = (now - pos.entry_time).total_seconds() / 60.0
    if held_min >= p.max_hold_minutes:
        return ExitDecision(True, "max_hold", 1.0)

    if pos.current_spread_pct >= p.exit_spread_pct:
        return ExitDecision(True, "spread_widened", 1.0)

    if pos.peak_price > 0:
        drawdown = (pos.peak_price - pos.current_price) / pos.peak_price
        if drawdown >= p.trailing_stop_pct:
            return ExitDecision(True, "trailing_stop", 1.0)

    # First take-profit: scale out once, then let the trail manage the rest.
    if not pos.scaled_out and gain >= p.take_profit_pct:
        return ExitDecision(True, "take_profit_scale", p.scale_out_fraction)

    return ExitDecision(False, None, 0.0)
