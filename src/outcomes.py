"""Forward-outcome computation. Pure — no I/O.

Given a runner's reference price (its session close) and the daily bars of the
following sessions, compute per-horizon forward return, max gain, and max
drawdown. These are the *outcomes* that let retrospection judge whether a given
parameter set would have entered the right names.
"""

from __future__ import annotations

from typing import Any


def compute_outcomes(
    ref_price: float,
    forward_bars: list[dict[str, Any]],
    horizons: tuple[int, ...] = (1, 3, 5),
) -> list[dict[str, Any]]:
    """Per-horizon outcomes from forward daily bars (ascending by date).

    Each bar needs `high`, `low`, `close`. A horizon is skipped if fewer than
    that many forward bars exist yet (data not available — re-run later fills it).
    `max_drawdown_pct` is the most negative low-vs-ref excursion in the window.
    """
    if ref_price <= 0 or not forward_bars:
        return []

    out: list[dict[str, Any]] = []
    for h in horizons:
        window = forward_bars[:h]
        if len(window) < h:
            continue
        highs = [b["high"] for b in window]
        lows = [b["low"] for b in window]
        fwd_price = window[-1]["close"]
        out.append(
            {
                "horizon": f"{h}d",
                "ref_price": ref_price,
                "fwd_price": fwd_price,
                "fwd_return_pct": (fwd_price - ref_price) / ref_price * 100,
                "max_gain_pct": (max(highs) - ref_price) / ref_price * 100,
                "max_drawdown_pct": (min(lows) - ref_price) / ref_price * 100,
            }
        )
    return out
