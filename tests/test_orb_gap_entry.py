"""Opening-range-breakout and gap-and-go long entries. Pure — no I/O.

Two continuation-flavored long shapes (unlike pullback's reversal). ORB buys the
break of the first-N-min range; gap-and-go buys the open on a qualifying gap-up
that's holding. Both select momentum/continuation rather than the fizzle-heavy
bulk of the crosser universe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.intraday import (
    reconstruct_gap_and_go_entry,
    reconstruct_orb_entry,
)

T0 = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)


def _bars(rows):  # (open, high, low, close)
    return [
        {
            "ts": T0 + timedelta(minutes=i),
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": 1000,
        }
        for i, (o, h, low, c) in enumerate(rows)
    ]


# ---- ORB ----


def test_orb_enters_on_break_of_opening_range_high():
    # opening range = first 2 bars, OR_high = 11.0; break above at idx3 (close 11.5)
    bars = _bars(
        [
            (10.0, 11.0, 10.0, 10.5),  # 0 OR
            (10.5, 11.0, 10.4, 10.8),  # 1 OR
            (10.8, 10.9, 10.6, 10.7),  # 2 inside range
            (10.9, 11.6, 10.9, 11.5),  # 3 breakout -> entry
        ]
    )
    idx = reconstruct_orb_entry(
        bars, prev_close=10.0, entry_min_change=5.0, min_price=1.0, max_price=50.0, orb_minutes=2
    )
    assert idx == 3


def test_orb_none_if_range_never_broken():
    bars = _bars([(10, 11, 10, 10.5), (10.5, 11, 10.4, 10.8), (10.6, 10.9, 10.5, 10.7)])
    assert reconstruct_orb_entry(bars, 10.0, 5.0, 1.0, 50.0, orb_minutes=2) is None


def test_orb_requires_runner_activation_at_entry():
    # breaks the OR high but only +2% vs prev_close -> not a +5% runner -> no entry
    bars = _bars([(10, 10.2, 10, 10.1), (10.1, 10.2, 10.0, 10.15), (10.15, 10.3, 10.1, 10.2)])
    assert reconstruct_orb_entry(bars, 10.0, 5.0, 1.0, 50.0, orb_minutes=2) is None


# ---- gap-and-go ----


def test_gap_and_go_enters_open_on_holding_gap():
    # opens +8% over prev_close 10 (=10.8) and holds green -> enter at bar 0
    bars = _bars([(10.8, 11.2, 10.7, 11.0), (11.0, 11.5, 10.9, 11.4)])
    idx = reconstruct_gap_and_go_entry(
        bars, 10.0, entry_min_change=5.0, min_price=1.0, max_price=50.0
    )
    assert idx == 0


def test_gap_and_go_none_if_gap_too_small():
    bars = _bars([(10.2, 10.4, 10.1, 10.3)])  # only +2% gap
    assert reconstruct_gap_and_go_entry(bars, 10.0, 5.0, 1.0, 50.0) is None


def test_gap_and_go_none_if_open_bar_is_red():
    # gaps +8% but the open bar fades (close < open) -> not "and go"
    bars = _bars([(10.8, 10.9, 9.9, 10.1)])
    assert reconstruct_gap_and_go_entry(bars, 10.0, 5.0, 1.0, 50.0) is None
