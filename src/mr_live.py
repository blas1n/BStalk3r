"""Live RSI-2 rebalance decisions. Pure — no I/O.

Runs once near the close. For each name we have recent daily closes plus today's
provisional close (current price); RSI-2 and the long SMA are computed on
`closes + [current]`. Exits fire when RSI bounces back or max-hold is reached;
entries fire on the oversold-dip-in-uptrend rule, capacity-capped and admitted
most-oversold-first. Returns the SELL and BUY symbol lists for the execution
layer to route (dry-run by default).
"""

from __future__ import annotations

from src.mean_reversion import rsi, sma


def _rsi_last(closes: list[float], current: float, period: int) -> float | None:
    r = rsi([*closes, current], period)
    return r[-1]


def mr_decisions(
    closes_by_symbol: dict[str, list[float]],
    current_price: dict[str, float],
    dollar_vol: dict[str, float],
    held: dict[str, int],
    rsi_period: int = 2,
    entry_rsi: float = 15.0,
    exit_rsi: float = 70.0,
    ma_period: int = 200,
    max_hold: int = 10,
    max_positions: int = 20,
    min_price: float = 5.0,
    max_price: float = 1000.0,
    min_dollar_vol: float = 0.0,
) -> dict[str, list[str]]:
    """Decide SELLs (bounce / max-hold) and BUYs (capacity-capped, most-oversold
    first) for today's close. `held` maps open-position symbol -> days held."""
    exits: list[str] = []
    for sym, days in held.items():
        closes = closes_by_symbol.get(sym)
        cur = current_price.get(sym)
        bounced = False
        if closes and cur is not None:
            r = _rsi_last(closes, cur, rsi_period)
            bounced = r is not None and r >= exit_rsi
        if bounced or days >= max_hold:
            exits.append(sym)

    remaining = len(held) - len(exits)
    free = max_positions - remaining
    candidates: list[tuple[str, float]] = []  # (symbol, rsi) — lower rsi = stronger
    if free > 0:
        for sym, closes in closes_by_symbol.items():
            if sym in held:
                continue
            cur = current_price.get(sym)
            if cur is None or not (min_price <= cur <= max_price):
                continue
            if dollar_vol.get(sym, 0.0) < min_dollar_vol:
                continue
            r = _rsi_last(closes, cur, rsi_period)
            if r is None or r > entry_rsi:
                continue
            if ma_period > 0:
                m = sma([*closes, cur], ma_period)[-1]
                if m is None or cur <= m:
                    continue
            candidates.append((sym, r))
    candidates.sort(key=lambda x: x[1])  # most oversold first
    entries = [sym for sym, _ in candidates[:free]]
    return {"exits": exits, "entries": entries}
