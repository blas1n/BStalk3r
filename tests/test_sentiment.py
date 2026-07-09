"""News-sentiment engine. Pure — no I/O.

Non-price alpha: score Polygon headlines with a finance lexicon, aggregate to a
daily per-ticker sentiment panel, then cross-sectionally long high-sentiment /
short low-sentiment names and hold N days. Tests the documented short-term
news-sentiment effect, executable on a liquid (borrowable) universe.
"""

from __future__ import annotations

from src.sentiment import (
    aggregate_sentiment,
    score_text,
    sentiment_backtest,
)


def test_score_text_positive_negative_neutral():
    assert score_text("Company beats estimates, shares surge to record high") > 0.5
    assert score_text("Firm misses guidance, stock plunges on weak sales") < -0.5
    assert score_text("The company held its annual meeting on Tuesday") == 0.0


def test_score_text_is_normalized_by_hits():
    # 2 positive, 1 negative -> (2-1)/(2+1) = +1/3
    s = score_text("beat surge miss")
    assert abs(s - (1 / 3)) < 1e-9


def test_aggregate_sentiment_by_ticker_and_date():
    articles = [
        {"date": "2026-06-01", "tickers": ["AAA", "BBB"], "score": 1.0},
        {"date": "2026-06-01", "tickers": ["AAA"], "score": -0.2},
        {"date": "2026-06-02", "tickers": ["AAA"], "score": 0.5},
    ]
    agg = aggregate_sentiment(articles)
    assert abs(agg[("AAA", "2026-06-01")]["mean"] - 0.4) < 1e-9  # (1.0 + -0.2)/2
    assert agg[("AAA", "2026-06-01")]["n"] == 2
    assert agg[("BBB", "2026-06-01")]["mean"] == 1.0
    assert agg[("AAA", "2026-06-02")]["mean"] == 0.5


def _panel(rows_by_date):
    return {
        d: {s: {"close": c, "dollar_vol": 1e8} for s, c in row.items()}
        for d, row in rows_by_date.items()
    }


def test_sentiment_backtest_longs_high_sentiment():
    # GOOD had positive news on d1, rises d1->d2; BAD had negative news, falls.
    price = _panel(
        {
            "2026-06-01": {"GOOD": 10.0, "BAD": 10.0},
            "2026-06-02": {"GOOD": 11.0, "BAD": 9.0},  # rebalance
            "2026-06-03": {"GOOD": 12.0, "BAD": 8.0},  # forward
        }
    )
    sent = aggregate_sentiment(
        [
            {"date": "2026-06-02", "tickers": ["GOOD"], "score": 0.8},
            {"date": "2026-06-02", "tickers": ["BAD"], "score": -0.8},
        ]
    )
    res = sentiment_backtest(
        price,
        sent,
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1,
        max_price=100,
        min_dollar_vol=0,
        min_articles=1,
    )
    assert len(res) == 1
    r = res[0]
    assert r.n_long == 1 and r.n_short == 1
    # long GOOD fwd = 12/11-1 = +9.1%; short BAD fwd = 8/9-1 = -11.1%
    assert abs(r.long_ret - (12 / 11 - 1)) < 1e-6
    assert abs(r.short_ret - (8 / 9 - 1)) < 1e-6


def test_sentiment_backtest_skips_thin_coverage():
    price = _panel(
        {
            "2026-06-01": {"A": 10.0, "B": 10.0},
            "2026-06-02": {"A": 11.0, "B": 9.0},
            "2026-06-03": {"A": 12.0, "B": 8.0},
        }
    )
    # only one ticker has news -> can't form both tails -> no rebalance
    sent = aggregate_sentiment([{"date": "2026-06-02", "tickers": ["A"], "score": 0.8}])
    res = sentiment_backtest(
        price,
        sent,
        formation_days=1,
        hold_days=1,
        quantile=0.5,
        min_price=1,
        max_price=100,
        min_dollar_vol=0,
        min_articles=1,
    )
    assert res == []
