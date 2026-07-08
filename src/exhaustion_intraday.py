"""H-B v2 — realistic intraday-trigger exhaustion short. Pure — no I/O.

v1 (`exhaustion.py`) shorted only days that *closed* red (EOD selection — not
knowable at the open) and filled at the red-day open (look-ahead). v2 removes
both biases:

- The short *candidate* is simply the session AFTER a qualifying parabolic run
  (cumulative gain over `run_days` ≥ `run_min_gain_pct`, run-end price in band),
  regardless of how that next day closes.
- The entry is reconstructed from that day's *minute* bars — the first intraday
  up-break of `entry_min_change`%% above the run-end close — then exited on the
  live short rules via `simulate_short_trade`. A day that never breaks up
  produces no trade; a green-open blow-off that fades is shorted like any other.

Split into two pure steps so each is independently testable and the SDK/minute
fetch stays in the CLI layer: `qualifying_run_ends` (daily → candidates) and
`simulate_run_end_short` (candidate + that day's minute bars → trade).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.intraday import (
    ShortTrade,
    reconstruct_breakdown_entry,
    simulate_short_trade,
)
from src.strategy import ExitParams


@dataclass(frozen=True)
class RunEnd:
    """A parabolic run that just ended — its next session is the short candidate."""

    symbol: str
    run_end_date: str
    short_day_date: str  # the session AFTER the run
    run_gain_pct: float
    prev_close: float  # run-end close = reference level for the intraday up-break


@dataclass(frozen=True)
class IntradayExhaustionShort:
    symbol: str
    short_day_date: str
    run_gain_pct: float
    trade: ShortTrade


def qualifying_run_ends(
    daily_by_symbol: dict[str, list[dict[str, Any]]],
    run_days: int,
    run_min_gain_pct: float,
    min_price: float,
    max_price: float,
) -> list[RunEnd]:
    """Every (parabolic run -> next session) short candidate across all symbols.

    A candidate at index i: cumulative gain over the prior `run_days` sessions ≥
    `run_min_gain_pct` and the run-end price in band. The candidate short day is
    seq[i+1] — with NO requirement on its color (that's the v1 bias this fixes).
    """
    out: list[RunEnd] = []
    for symbol, seq in daily_by_symbol.items():
        for i in range(run_days, len(seq) - 1):
            run_start = seq[i - run_days]
            run_end = seq[i]
            if not run_start.get("close") or not run_end.get("close"):
                continue
            gain = (run_end["close"] - run_start["close"]) / run_start["close"] * 100
            if gain < run_min_gain_pct:
                continue
            if not (min_price <= run_end["close"] <= max_price):
                continue
            short_day = seq[i + 1]
            out.append(
                RunEnd(
                    symbol=symbol,
                    run_end_date=str(run_end.get("date", "")),
                    short_day_date=str(short_day.get("date", "")),
                    run_gain_pct=gain,
                    prev_close=float(run_end["close"]),
                )
            )
    return out


def simulate_run_end_short(
    run_end: RunEnd,
    minute_bars: list[dict[str, Any]],
    entry_min_change: float,
    min_price: float,
    max_price: float,
    exit_params: ExitParams,
    cost_fn: Callable[[float], float] | None = None,
    entry_mode: str = "breakout",
) -> IntradayExhaustionShort | None:
    """Short the run-end day via the live short rules; None if it never triggers.

    `entry_min_change` is measured against `run_end.prev_close`. `entry_mode`:
    - "breakout" (default): fade the intraday up-break (short into strength —
      momentum-fighting; empirically the losing entry).
    - "breakdown": short the loss of the prior close (down-break — the
      exhaustion/first-red-day thesis; shorting weakness).
    """
    if entry_mode == "breakdown":
        idx = reconstruct_breakdown_entry(
            minute_bars, run_end.prev_close, entry_min_change, min_price, max_price
        )
        if idx is None:
            return None  # never lost the prior close — no breakdown short
        trade = simulate_short_trade(
            minute_bars,
            run_end.prev_close,
            entry_min_change,
            min_price,
            max_price,
            exit_params,
            cost_fn,
            entry_idx=idx,
        )
    else:
        trade = simulate_short_trade(
            minute_bars,
            run_end.prev_close,
            entry_min_change,
            min_price,
            max_price,
            exit_params,
            cost_fn,
        )
    if not trade.entered:
        return None
    return IntradayExhaustionShort(
        symbol=run_end.symbol,
        short_day_date=run_end.short_day_date,
        run_gain_pct=run_end.run_gain_pct,
        trade=trade,
    )
