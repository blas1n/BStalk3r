"""First-red-day / multi-day exhaustion short (H-B). Pure — daily sequences only.

Targets the practitioner + academic edge: a stock that ran up parabolically over
N days and then prints its FIRST red day (closes below the prior close) tends to
fade. Unlike H-A (short at the day-1 +X% cross, blended to breakeven because you
can't tell fizzles from survivors), this waits for the run to *exhaust and break*
— which is exactly when the survivors reveal themselves.

This is a fast concept test on grouped daily bars (no minute fetches). If the
edge shows, refine to an intraday entry on the red day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExhaustionShort:
    symbol: str
    run_end_date: str
    red_day_date: str
    run_gain_pct: float
    intraday_short_ret: float  # short red-day open -> red-day close, net %
    swing_short_ret: float | None  # short red-day close -> +fwd_days close, net %


def find_exhaustion_shorts(
    daily_by_symbol: dict[str, list[dict[str, Any]]],
    run_days: int,
    run_min_gain_pct: float,
    min_price: float,
    max_price: float,
    fwd_days: int,
    cost_pct: float,
) -> list[ExhaustionShort]:
    """Every (parabolic run -> first red day) short setup across all symbols.

    A setup at index i: cumulative gain over the prior `run_days` sessions ≥
    `run_min_gain_pct`, run-end price in band, and the NEXT session closes red.
    Reports the red-day intraday short (open->close) and the swing short
    (red close -> +fwd_days close), both net of `cost_pct` round-trip.
    """
    out: list[ExhaustionShort] = []
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
            red = seq[i + 1]
            if not red.get("close") or red["close"] >= run_end["close"]:
                continue  # not a red day

            intraday = (red["open"] - red["close"]) / red["open"] * 100 - cost_pct
            swing: float | None = None
            j = i + 1 + fwd_days
            if j < len(seq) and seq[j].get("close"):
                swing = (red["close"] - seq[j]["close"]) / red["close"] * 100 - cost_pct
            out.append(
                ExhaustionShort(
                    symbol=symbol,
                    run_end_date=str(run_end.get("date", "")),
                    red_day_date=str(red.get("date", "")),
                    run_gain_pct=gain,
                    intraday_short_ret=intraday,
                    swing_short_ret=swing,
                )
            )
    return out
