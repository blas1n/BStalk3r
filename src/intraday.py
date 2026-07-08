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


def entry_features(
    bars: list[dict[str, Any]], entry_idx: int, prev_close: float
) -> dict[str, float]:
    """Features observable AT the cross moment — from bars[:entry_idx+1] only.

    No look-ahead: everything here a live scanner could compute the instant the
    trigger fires, to try to tell future winners from fizzles.
    """
    upto = bars[: entry_idx + 1]
    vols = [b.get("volume", 0) or 0 for b in upto]
    cum_vol = sum(vols)
    entry_price = upto[-1]["close"]
    open_price = bars[0]["open"] if bars else entry_price
    avg_vol = cum_vol / len(upto) if upto else 0
    return {
        "cum_volume": float(cum_vol),
        "cum_dollar_vol": float(cum_vol * entry_price),
        "minutes_to_cross": float(entry_idx),  # RTH bars are 1-min from the open
        "gap_pct": (open_price - prev_close) / prev_close * 100 if prev_close else 0.0,
        "vol_accel": (vols[-1] / avg_vol) if avg_vol else 1.0,
        "entry_price": float(entry_price),
    }


def bucket_by_feature(
    samples: list[tuple[dict[str, float], float]], feature_key: str, n_buckets: int = 3
) -> list[dict[str, Any]]:
    """Sort trades by one entry-feature, split into equal buckets, and report
    each bucket's net-return stats. A big low->high spread means the feature
    separates winners from losers at entry (a usable real-time filter).

    `samples` = list of (features, net_return_pct).
    """
    valid = [(f[feature_key], r) for f, r in samples if feature_key in f]
    if len(valid) < n_buckets:
        return []
    valid.sort(key=lambda x: x[0])
    labels = ["low", "mid", "high"] if n_buckets == 3 else [f"q{i}" for i in range(n_buckets)]
    size = len(valid) // n_buckets
    out: list[dict[str, Any]] = []
    for b in range(n_buckets):
        lo = b * size
        hi = len(valid) if b == n_buckets - 1 else (b + 1) * size
        chunk = valid[lo:hi]
        rets = [r for _, r in chunk]
        out.append(
            {
                "bucket": labels[b],
                "n": len(chunk),
                "lo": chunk[0][0],
                "hi": chunk[-1][0],
                "avg": sum(rets) / len(rets),
                "win_rate": sum(1 for x in rets if x > 0) / len(rets) * 100,
            }
        )
    return out


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
