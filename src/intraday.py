"""Intraday hit-and-run backtest. Pure — no I/O.

Reconstructs a realistic intraday entry from minute bars (first time the runner
crosses the entry trigger during the session) and then walks the bars applying
the *live* exit rules (`strategy.evaluate_exit`: stop / take-profit / trailing /
max-hold / force-close-at-bell). Reusing evaluate_exit means this backtest can
never drift from what the realtime loop would actually do.

Honest caveats:
- We only simulate stocks already known (from EOD screening) to have run that
  day — survivorship-optimistic vs a live intraday scanner that also fires on
  pops that immediately died.
- Fills are modeled at the triggering bar's close (no intrabar slippage beyond
  the configured round-trip cost). Take-profit is a full exit (no partial
  scale-out) in this v1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.models import PositionState
from src.strategy import ExitParams, evaluate_exit


@dataclass(frozen=True)
class IntradayTrade:
    entered: bool
    entry_price: float = 0.0
    entry_time: Any = None
    exit_price: float = 0.0
    exit_time: Any = None
    exit_reason: str | None = None
    held_min: float = 0.0
    gross_return_pct: float = 0.0
    net_return_pct: float = 0.0


def aggregate(trades: list[IntradayTrade]) -> dict[str, Any] | None:
    """Net-return stats over a set of trades (None if empty)."""
    if not trades:
        return None
    nets = [t.net_return_pct for t in trades]
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason or "?"] = reasons.get(t.exit_reason or "?", 0) + 1
    return {
        "n": len(trades),
        "avg": sum(nets) / len(nets),
        "median": sorted(nets)[len(nets) // 2],
        "win_rate": sum(1 for x in nets if x > 0) / len(nets) * 100,
        "avg_hold": sum(t.held_min for t in trades) / len(trades),
        "reasons": reasons,
    }


def reconstruct_entry(
    bars: list[dict[str, Any]],
    prev_close: float,
    entry_min_change: float,
    min_price: float,
    max_price: float,
) -> int | None:
    """Index of the first bar where the runner trigger fires, else None.

    Trigger = intraday day-change (vs prior close) ≥ `entry_min_change` and the
    price inside the band. Bars are assumed already restricted to regular hours.
    """
    if prev_close <= 0:
        return None
    for i, bar in enumerate(bars):
        price = bar["close"]
        change = (price - prev_close) / prev_close * 100
        if change >= entry_min_change and min_price <= price <= max_price:
            return i
    return None


def simulate_trade(
    bars: list[dict[str, Any]],
    prev_close: float,
    entry_min_change: float,
    min_price: float,
    max_price: float,
    exit_params: ExitParams,
    cost_fn: Callable[[float], float] | None = None,
) -> IntradayTrade:
    """Enter at the trigger, exit on the first live exit signal; net of cost."""
    cost = cost_fn or (lambda price: 0.0)
    entry_idx = reconstruct_entry(bars, prev_close, entry_min_change, min_price, max_price)
    if entry_idx is None:
        return IntradayTrade(entered=False)

    entry_price = bars[entry_idx]["close"]
    entry_time = bars[entry_idx]["ts"]
    peak = entry_price
    last = len(bars) - 1

    exit_price, exit_time, reason, held = entry_price, entry_time, "session_end", 0.0
    for j in range(entry_idx + 1, len(bars)):
        bar = bars[j]
        peak = max(peak, bar["high"])
        held = (bar["ts"] - entry_time).total_seconds() / 60.0
        state = PositionState(
            symbol="",
            entry_price=entry_price,
            qty=1,
            entry_time=entry_time,
            current_price=bar["close"],
            peak_price=peak,
            current_spread_pct=0.0,
            scaled_out=False,
        )
        decision = evaluate_exit(state, exit_params, now=bar["ts"], force_close=(j == last))
        if decision.should_exit:
            exit_price, exit_time, reason = bar["close"], bar["ts"], decision.reason
            break

    gross = (exit_price - entry_price) / entry_price * 100
    return IntradayTrade(
        entered=True,
        entry_price=entry_price,
        entry_time=entry_time,
        exit_price=exit_price,
        exit_time=exit_time,
        exit_reason=reason,
        held_min=held,
        gross_return_pct=gross,
        net_return_pct=gross - cost(entry_price),
    )


@dataclass(frozen=True)
class ShortTrade:
    entered: bool
    entry_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str | None = None
    held_min: float = 0.0
    gross_return_pct: float = 0.0
    net_return_pct: float = 0.0
    max_adverse_pct: float = 0.0  # worst up-excursion from entry (the squeeze tail)


def simulate_short_trade(
    bars: list[dict[str, Any]],
    prev_close: float,
    entry_min_change: float,
    min_price: float,
    max_price: float,
    exit_params: ExitParams,
    cost_fn: Callable[[float], float] | None = None,
) -> ShortTrade:
    """Fade: SHORT the +X% crosser, inverted exits. Stop = adverse UP move
    (triggered on bar HIGH, filled at the stop level — the squeeze risk);
    take-profit = favorable DOWN move (bar LOW); trailing tracks the trough.

    `max_adverse_pct` records the worst up-excursion regardless of the stop, so a
    violent intrabar squeeze (fill far worse than the assumed stop) is visible.
    """
    cost = cost_fn or (lambda price: 0.0)
    entry_idx = reconstruct_entry(bars, prev_close, entry_min_change, min_price, max_price)
    if entry_idx is None:
        return ShortTrade(entered=False)

    entry = bars[entry_idx]["close"]
    entry_time = bars[entry_idx]["ts"]
    stop_level = entry * (1 + exit_params.stop_loss_pct)  # adverse (up)
    tp_level = entry * (1 - exit_params.take_profit_pct)  # favorable (down)
    trough = entry
    last = len(bars) - 1

    exit_price, reason, held, max_adverse = entry, "session_end", 0.0, 0.0
    for j in range(entry_idx + 1, len(bars)):
        bar = bars[j]
        max_adverse = max(max_adverse, (bar["high"] - entry) / entry * 100)
        held = (bar["ts"] - entry_time).total_seconds() / 60.0
        # risk first: adverse up-move (stop) before favorable (take-profit)
        if bar["high"] >= stop_level:
            exit_price, reason = stop_level, "stop_loss"
            break
        if bar["low"] <= tp_level:
            exit_price, reason = tp_level, "take_profit"
            break
        trough = min(trough, bar["low"])
        if bar["close"] >= trough * (1 + exit_params.trailing_stop_pct):
            exit_price, reason = bar["close"], "trailing_stop"
            break
        if held >= exit_params.max_hold_minutes or j == last:
            exit_price = bar["close"]
            reason = "max_hold" if held >= exit_params.max_hold_minutes else "force_close"
            break

    gross = (entry - exit_price) / entry * 100  # short profits when price falls
    return ShortTrade(
        entered=True,
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=reason,
        held_min=held,
        gross_return_pct=gross,
        net_return_pct=gross - cost(entry),
        max_adverse_pct=max_adverse,
    )
