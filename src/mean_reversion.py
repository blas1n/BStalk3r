"""Short-term mean-reversion engine (Connors RSI-2 style). Pure — no I/O.

Per symbol, buy a 1-2 day panic (RSI-`n` oversold) while in an uptrend (close >
long SMA), exit on the bounce (RSI back above a threshold) or after `max_hold`.
This is a time-series, per-symbol timing strategy (behavioral overreaction) —
distinct from the cross-sectional ranking that came up empty. The long SMA regime
filter is what keeps it from buying falling knives in downtrends.
"""

from __future__ import annotations

from typing import Any


def sma(closes: list[float], period: int) -> list[float | None]:
    """Trailing simple moving average; None until `period` values are available."""
    out: list[float | None] = [None] * len(closes)
    if period <= 0:
        return out
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1 : i + 1]) / period
    return out


def rsi(closes: list[float], period: int) -> list[float | None]:
    """Simple-average RSI over `period`; None during warmup. Two straight down
    days at period 2 -> 0 (fully oversold); two up days -> 100."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    for i in range(period, len(closes)):
        g = sum(gains[i - period : i]) / period
        loss = sum(losses[i - period : i]) / period
        if loss == 0:
            out[i] = 100.0
        else:
            rs = g / loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def mean_reversion_trades(
    dates: list[str],
    closes: list[float],
    dollar_vols: list[float],
    rsi_period: int = 2,
    entry_rsi: float = 10.0,
    exit_rsi: float = 50.0,
    ma_period: int = 200,
    max_hold: int = 5,
    min_price: float = 5.0,
    max_price: float = 1000.0,
    min_dollar_vol: float = 0.0,
    cost_frac: float = 0.0,
) -> list[dict[str, Any]]:
    """Walk one symbol's daily series; long the oversold dip in an uptrend, exit
    on the bounce or max-hold. Returns a list of trades (net of round-trip cost).

    Entry at bar i: RSI ≤ `entry_rsi`, in band, liquid, and (ma_period=0 or
    close > SMA). Exit at the first later bar with RSI ≥ `exit_rsi`, or after
    `max_hold` bars (fill at that bar's close)."""
    r = rsi(closes, rsi_period)
    m = sma(closes, ma_period) if ma_period > 0 else [None] * len(closes)
    trades: list[dict[str, Any]] = []
    i = 0
    n = len(closes)
    while i < n:
        price = closes[i]
        regime_ok = ma_period <= 0 or (m[i] is not None and price > m[i])
        if (
            r[i] is not None
            and r[i] <= entry_rsi
            and regime_ok
            and min_price <= price <= max_price
            and dollar_vols[i] >= min_dollar_vol
        ):
            entry_price = price
            exit_idx = None
            for j in range(i + 1, n):
                if (r[j] is not None and r[j] >= exit_rsi) or (j - i) >= max_hold:
                    exit_idx = j
                    break
            if exit_idx is None:  # ran out of data holding
                break
            exit_price = closes[exit_idx]
            gross = exit_price / entry_price - 1.0
            trades.append(
                {
                    "entry_date": dates[i],
                    "exit_date": dates[exit_idx],
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "ret": gross - 2 * cost_frac,
                    "held": exit_idx - i,
                }
            )
            i = exit_idx + 1  # no overlapping positions in one symbol
        else:
            i += 1
    return trades


def summarize_mr(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Net trade-return stats (None if empty)."""
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    n = len(rets)
    avg = sum(rets) / n
    var = sum((x - avg) ** 2 for x in rets) / n
    std = var**0.5
    return {
        "n": n,
        "avg": avg,
        "median": sorted(rets)[n // 2],
        "win": sum(1 for x in rets if x > 0) / n * 100,
        "std": std,
        "sharpe": (avg / std) if std else 0.0,
    }
