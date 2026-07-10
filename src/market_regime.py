"""Market vol-regime filter (VIX proxy). Pure — no I/O.

Proxies VIX with SPY's rolling realized volatility (free daily data, already in
the panel): annualized standard deviation of daily returns over `window`. Entries
are gated to *calm* dates (vol below a threshold) so RSI-2 mean reversion doesn't
buy falling knives in a crash.
"""

from __future__ import annotations

_ANN = 252**0.5


def realized_vol(closes: list[float], window: int) -> list[float | None]:
    """Annualized rolling realized volatility; None during warmup."""
    out: list[float | None] = [None] * len(closes)
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        rets.append(closes[i] / prev - 1.0 if prev else 0.0)
    for i in range(window, len(closes)):
        w = rets[i - window : i]  # `window` returns ending at date i
        mean = sum(w) / len(w)
        var = sum((x - mean) ** 2 for x in w) / len(w)
        out[i] = (var**0.5) * _ANN
    return out


def calm_dates(dates: list[str], closes: list[float], window: int, max_vol: float) -> set[str]:
    """Dates whose realized vol ≤ `max_vol` (calm regime). Warmup dates excluded
    (regime unknown)."""
    vol = realized_vol(closes, window)
    return {dates[i] for i in range(len(dates)) if vol[i] is not None and vol[i] <= max_vol}
