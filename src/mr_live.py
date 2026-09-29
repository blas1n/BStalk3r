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
    rank_dollar_vol: dict[str, float] | None = None,
) -> dict[str, list[str]]:
    """Decide SELLs (bounce / max-hold) and BUYs (capacity-capped, most-oversold
    first) for today's close. `held` maps open-position symbol -> days held.

    `dollar_vol` is the liquidity filter input (last session). `rank_dollar_vol`
    is the R5 tie-break key (20-day average dollar volume, D-1..D-20): candidates
    sort by RSI ascending, then rank_dollar_vol descending (missing -> last), then
    symbol ascending, so the pick is deterministic regardless of input order."""
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
    adv = rank_dollar_vol or {}

    def _key(c: tuple[str, float]) -> tuple[float, int, float, str]:
        sym, r = c
        v = adv.get(sym)
        # most oversold first; then larger 20d avg $vol; unknown ADV after known
        return (r, 0 if v is not None else 1, -(v or 0.0), sym)

    candidates.sort(key=_key)
    entries = [sym for sym, _ in candidates[:free]]
    return {"exits": exits, "entries": entries}


def trailing_avg_dollar_vol(
    panel: dict[str, dict[str, dict[str, float]]], today: str, window: int = 20
) -> dict[str, float]:
    """Average dollar volume over the `window` most recent sessions strictly before
    `today` (D-1..D-window), per symbol — the R5 tie-break key.

    Matches research/tiebreak ADV: a session in the window where the symbol has no
    bar contributes 0 and the divisor is the number of sessions in the window. If
    fewer than `window` prior sessions exist in the panel, it averages over the
    sessions that do exist (the simulator leaves ADV undefined there; live never
    hits this with its ~210-session lookback)."""
    sessions = sorted(d for d in panel if d < today)[-window:]
    if not sessions:
        return {}
    totals: dict[str, float] = {}
    for d in sessions:
        for sym, rec in panel[d].items():
            totals[sym] = totals.get(sym, 0.0) + float(rec.get("dollar_vol", 0.0) or 0.0)
    return {sym: tot / len(sessions) for sym, tot in totals.items()}
