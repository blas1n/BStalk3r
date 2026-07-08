"""Simulated short-accumulation record builder. Pure — no I/O.

Alpaca cannot short our target low-float runners (probed: 9/9 not shortable,
APIError 42210000 "cannot be sold short"), so live paper-shorting is a dead end.
Instead we accumulate a forward, out-of-time dataset: for each short setup from
either strategy we record the entry-time features, the *would-be* intraday short
outcome (via the live short rules), and the live shortable/easy_to_borrow status
— answering both "did the short work" and "was it even executable".

`build_short_record` unifies both strategies (fade / exhaustion) and both entry
modes (breakout up-break / breakdown prior-close loss) into one record; the
grouped/minute/Alpaca I/O lives in the CLI layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.exhaustion_intraday import RunEnd
from src.intraday import (
    entry_features,
    reconstruct_breakdown_entry,
    reconstruct_entry,
    simulate_short_trade,
)
from src.strategy import ExitParams


@dataclass(frozen=True)
class ShortSetup:
    """A detected short setup, strategy-agnostic. `ref_close` is the reference the
    intraday trigger is measured against (prior/run-end close)."""

    symbol: str
    session_date: str
    strategy: str  # "fade" | "exhaustion"
    ref_close: float
    trigger_pct: float
    entry_mode: str  # "breakout" | "breakdown"
    run_gain_pct: float | None = None  # exhaustion context
    is_fizzle: bool | None = None  # fade context (crosser labeling)


@dataclass(frozen=True)
class ShortSetupRecord:
    symbol: str
    session_date: str
    strategy: str
    entry_mode: str
    triggered: bool
    entry_price: float
    net_return_pct: float
    exit_reason: str | None
    held_min: float
    max_adverse_pct: float
    shortable: bool
    easy_to_borrow: bool
    run_gain_pct: float | None
    is_fizzle: bool | None
    features: dict[str, float] = field(default_factory=dict)


def fade_setups(
    crossers: list[dict[str, Any]], session_date: str, trigger_pct: float
) -> list[ShortSetup]:
    """Map H-A crossers (`polygon_grouped_crossers` output) to fade shorts —
    breakout entry (short the intraday up-cross), carrying the fizzle label."""
    return [
        ShortSetup(
            symbol=c["symbol"],
            session_date=session_date,
            strategy="fade",
            ref_close=float(c["prev_close"]),
            trigger_pct=trigger_pct,
            entry_mode="breakout",
            is_fizzle=c.get("is_fizzle"),
        )
        for c in crossers
    ]


def exhaustion_setups(
    run_ends: list[RunEnd], trigger_pct: float, entry_mode: str = "breakdown"
) -> list[ShortSetup]:
    """Map H-B run-ends (`qualifying_run_ends` output) to exhaustion shorts on the
    session AFTER the run, carrying the run-gain context."""
    return [
        ShortSetup(
            symbol=e.symbol,
            session_date=e.short_day_date,
            strategy="exhaustion",
            ref_close=e.prev_close,
            trigger_pct=trigger_pct,
            entry_mode=entry_mode,
            run_gain_pct=e.run_gain_pct,
        )
        for e in run_ends
    ]


def build_short_record(
    setup: ShortSetup,
    minute_bars: list[dict[str, Any]],
    exit_params: ExitParams,
    shortable: bool,
    easy_to_borrow: bool,
    cost_fn: Callable[[float], float] | None = None,
    min_price: float = 0.0,
    max_price: float = 1e9,
) -> ShortSetupRecord | None:
    """Build one forward-dataset record for a short setup, or None if the day
    never triggers an entry. Entry uses the setup's `entry_mode` (breakout =
    up-break of `ref_close`; breakdown = loss of `ref_close`); the *would-be*
    short outcome comes from the live short rules; entry-time features are
    computed from bars[:entry_idx+1] only (no look-ahead)."""
    if setup.entry_mode == "breakdown":
        entry_idx = reconstruct_breakdown_entry(
            minute_bars, setup.ref_close, setup.trigger_pct, min_price, max_price
        )
    else:
        entry_idx = reconstruct_entry(
            minute_bars, setup.ref_close, setup.trigger_pct, min_price, max_price
        )
    if entry_idx is None:
        return None

    trade = simulate_short_trade(
        minute_bars,
        setup.ref_close,
        setup.trigger_pct,
        min_price,
        max_price,
        exit_params,
        cost_fn,
        entry_idx=entry_idx,
    )
    if not trade.entered:
        return None

    feats = entry_features(minute_bars, entry_idx, setup.ref_close)
    return ShortSetupRecord(
        symbol=setup.symbol,
        session_date=setup.session_date,
        strategy=setup.strategy,
        entry_mode=setup.entry_mode,
        triggered=True,
        entry_price=trade.entry_price,
        net_return_pct=trade.net_return_pct,
        exit_reason=trade.exit_reason,
        held_min=trade.held_min,
        max_adverse_pct=trade.max_adverse_pct,
        shortable=shortable,
        easy_to_borrow=easy_to_borrow,
        run_gain_pct=setup.run_gain_pct,
        is_fizzle=setup.is_fizzle,
        features=feats,
    )
