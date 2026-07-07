"""Survivorship-inclusive crosser enumeration from grouped HIGH.

The current screener filters by CLOSE, so it only sees stocks that *ended* the
day up — it misses "fizzles" (popped +X% intraday then faded). A live scanner
fires on every intraday cross, so the honest backtest must include fizzles.
`polygon_grouped_crossers` uses the grouped HIGH to enumerate them all.
"""

from __future__ import annotations

from src.sources import polygon_grouped_crossers

TODAY = [
    {"T": "WINN", "c": 11.0, "h": 12.0},  # +10% close, +20% high -> survivor
    {"T": "FIZZ", "c": 9.0, "h": 10.6},  # -10% close, +6% high  -> fizzle
    {"T": "CRASH", "c": 0.7, "h": 10.5},  # closed cheap but touched +5% -> fizzle, still enterable
    {"T": "NOCROSS", "c": 10.1, "h": 10.2},  # +2% high -> not a crosser
    {"T": "RICH", "c": 105.0, "h": 120.0},  # entry est > $50 band -> excluded
]
PREV = {
    "WINN": {"c": 10.0},
    "FIZZ": {"c": 10.0},
    "CRASH": {"c": 10.0},
    "NOCROSS": {"c": 10.0},
    "RICH": {"c": 100.0},
}


def _by(rows):
    return {r["symbol"]: r for r in rows}


def test_enumerates_all_intraday_crossers_including_fizzles():
    rows = polygon_grouped_crossers(TODAY, PREV, min_price=1.0, max_price=50.0, entry_trigger=5.0)
    by = _by(rows)
    assert set(by) == {"WINN", "FIZZ", "CRASH"}  # NOCROSS + RICH excluded
    assert by["WINN"]["is_fizzle"] is False
    assert by["FIZZ"]["is_fizzle"] is True
    # CRASH touched +5% intraday but closed at -30% -> fizzle, kept (was tradeable at entry)
    assert by["CRASH"]["is_fizzle"] is True


def test_fizzle_share_matches_close_vs_high():
    rows = polygon_grouped_crossers(TODAY, PREV, 1.0, 50.0, 5.0)
    fizzles = [r for r in rows if r["is_fizzle"]]
    assert len(fizzles) == 2  # FIZZ + CRASH
    assert len(rows) == 3


def test_band_filters_on_entry_price_not_close():
    # RICH: prev 100 -> entry ~105 > 50 -> excluded even though it crossed
    rows = polygon_grouped_crossers(TODAY, PREV, 1.0, 50.0, 5.0)
    assert "RICH" not in _by(rows)


def test_carries_prev_close_for_downstream_sim():
    rows = polygon_grouped_crossers(TODAY, PREV, 1.0, 50.0, 5.0)
    assert _by(rows)["WINN"]["prev_close"] == 10.0


def test_missing_prev_or_fields_skipped():
    today = [{"T": "X", "c": 11.0, "h": 12.0}, {"T": "Y", "c": None, "h": 12.0}]
    rows = polygon_grouped_crossers(today, {}, 1.0, 50.0, 5.0)  # no prev at all
    assert rows == []
