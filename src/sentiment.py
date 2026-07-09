"""News-sentiment engine. Pure — no I/O.

Non-price alpha. Score headlines with a compact finance lexicon (Loughran-McDonald
flavored), aggregate to a daily per-ticker sentiment panel, then cross-sectionally
long the highest-sentiment names and short the lowest, holding N days. Reuses the
cross-sectional `RebalanceResult`/`summarize_rebalances` so results are directly
comparable to the momentum/reversal search. Executable on a liquid (borrowable)
universe.
"""

from __future__ import annotations

import re
from typing import Any

from src.xsectional import RebalanceResult

# Compact finance-news sentiment lexicon (headline-oriented).
_POS = {
    "beat",
    "beats",
    "surge",
    "surged",
    "surges",
    "soar",
    "soared",
    "jump",
    "jumps",
    "jumped",
    "rally",
    "rallied",
    "gain",
    "gains",
    "gained",
    "upgrade",
    "upgraded",
    "record",
    "growth",
    "profit",
    "profits",
    "strong",
    "boost",
    "boosted",
    "outperform",
    "bullish",
    "rise",
    "rises",
    "rose",
    "top",
    "tops",
    "topped",
    "exceed",
    "exceeds",
    "exceeded",
    "higher",
    "win",
    "wins",
    "approval",
    "approved",
    "breakthrough",
    "positive",
    "optimistic",
    "raise",
    "raises",
    "raised",
    "expand",
    "expands",
    "soars",
    "upbeat",
    "rebound",
    "rebounds",
    "climbs",
    "climb",
}
_NEG = {
    "miss",
    "misses",
    "missed",
    "plunge",
    "plunged",
    "plunges",
    "drop",
    "drops",
    "dropped",
    "fall",
    "falls",
    "fell",
    "slump",
    "slumped",
    "decline",
    "declines",
    "declined",
    "downgrade",
    "downgraded",
    "loss",
    "losses",
    "weak",
    "cut",
    "cuts",
    "slash",
    "slashed",
    "warn",
    "warning",
    "warned",
    "bearish",
    "lower",
    "sink",
    "sinks",
    "sank",
    "tumble",
    "tumbles",
    "tumbled",
    "fear",
    "fears",
    "concern",
    "concerns",
    "lawsuit",
    "probe",
    "investigation",
    "recall",
    "bankruptcy",
    "default",
    "layoff",
    "layoffs",
    "negative",
    "disappoint",
    "disappointing",
    "sell-off",
    "selloff",
    "crash",
    "halts",
    "halt",
    "fraud",
    "slide",
    "slides",
}
_WORD = re.compile(r"[a-z][a-z\-]+")


def score_text(text: str) -> float:
    """Net finance-lexicon sentiment of `text`, normalized to [-1, 1] by hits.
    (pos − neg) / (pos + neg); 0 when no sentiment words appear."""
    pos = neg = 0
    for w in _WORD.findall((text or "").lower()):
        if w in _POS:
            pos += 1
        elif w in _NEG:
            neg += 1
    total = pos + neg
    return (pos - neg) / total if total else 0.0


def aggregate_sentiment(
    articles: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """{(ticker, date): {mean, n}} — each article's score attributed to every
    ticker it mentions on its date."""
    acc: dict[tuple[str, str], list[float]] = {}
    for a in articles:
        for t in a.get("tickers", []):
            acc.setdefault((t, a["date"]), []).append(a["score"])
    return {k: {"mean": sum(v) / len(v), "n": len(v)} for k, v in acc.items()}


def _recent_sentiment(
    sentiment: dict[tuple[str, str], dict[str, Any]],
    symbol: str,
    dates: list[str],
    end_idx: int,
    formation_days: int,
) -> tuple[float, int] | None:
    """Mean sentiment for `symbol` over the `formation_days` sessions ending at
    end_idx (inclusive); None if no articles in the window."""
    scores, n = [], 0
    for k in range(max(0, end_idx - formation_days + 1), end_idx + 1):
        rec = sentiment.get((symbol, dates[k]))
        if rec:
            scores.append(rec["mean"] * rec["n"])
            n += rec["n"]
    if n == 0:
        return None
    return sum(scores) / n, n


def sentiment_backtest(
    price_panel: dict[str, dict[str, dict[str, float]]],
    sentiment: dict[tuple[str, str], dict[str, Any]],
    formation_days: int,
    hold_days: int,
    quantile: float,
    min_price: float,
    max_price: float,
    min_dollar_vol: float,
    min_articles: int,
) -> list[RebalanceResult]:
    """Non-overlapping cross-sectional sentiment backtest: rank eligible liquid
    names by recent news sentiment, long the top / short the bottom `quantile`,
    hold `hold_days`, record each basket's forward return."""
    dates = sorted(price_panel)
    out: list[RebalanceResult] = []
    i = formation_days - 1
    while i + hold_days < len(dates):
        d, d1 = dates[i], dates[i + hold_days]
        ranked: list[tuple[str, float, float]] = []  # (symbol, sentiment, fwd_ret)
        for sym, cur in price_panel[d].items():
            fut = price_panel[d1].get(sym)
            if not fut:
                continue
            price = cur["close"]
            if not (min_price <= price <= max_price) or cur["dollar_vol"] < min_dollar_vol:
                continue
            rs = _recent_sentiment(sentiment, sym, dates, i, formation_days)
            if rs is None or rs[1] < min_articles or price <= 0:
                continue
            ranked.append((sym, rs[0], fut["close"] / price - 1.0))

        n = len(ranked)
        k = max(1, int(n * quantile))
        if n < 2 * k:
            i += hold_days
            continue
        ranked.sort(key=lambda x: x[1])
        low, high = ranked[:k], ranked[-k:]  # low/high sentiment
        long_ret = sum(x[2] for x in high) / k  # long high sentiment
        short_ret = sum(x[2] for x in low) / k  # short low sentiment
        out.append(
            RebalanceResult(date=d, long_ret=long_ret, short_ret=short_ret, n_long=k, n_short=k)
        )
        i += hold_days
    return out
