"""Summary over accumulated short_setups rows. Pure — no I/O.

Answers the live question: does the *executable* (shortable) subset of either
short strategy behave differently from the un-borrowable bulk? Groups the
would-be outcomes by (strategy, shortable) plus an overall roll-up.
"""

from __future__ import annotations

from src.short_report import summarize_short_setups


def _row(strategy, shortable, net, session="2026-07-06"):
    return {
        "strategy": strategy,
        "shortable": 1 if shortable else 0,
        "net_return_pct": net,
        "session_date": session,
    }


def test_empty_is_none():
    assert summarize_short_setups([]) is None


def test_groups_by_strategy_and_borrowability():
    rows = [
        _row("fade", True, 4.0),
        _row("fade", True, -2.0),
        _row("fade", False, -6.0),
        _row("exhaustion", False, -3.0),
    ]
    s = summarize_short_setups(rows)
    assert s["n"] == 4
    assert s["sessions"] == 1
    groups = {(g["strategy"], g["shortable"]): g for g in s["groups"]}
    fade_short = groups[("fade", True)]
    assert fade_short["n"] == 2
    assert abs(fade_short["avg"] - 1.0) < 1e-9  # (4 + -2)/2
    assert abs(fade_short["win"] - 50.0) < 1e-9  # 1 of 2 positive
    assert groups[("fade", False)]["n"] == 1
    assert groups[("exhaustion", False)]["avg"] == -3.0


def test_counts_distinct_sessions():
    rows = [
        _row("fade", True, 1.0, session="2026-07-06"),
        _row("fade", True, 2.0, session="2026-07-07"),
        _row("fade", True, 3.0, session="2026-07-07"),
    ]
    s = summarize_short_setups(rows)
    assert s["sessions"] == 2
    assert s["n"] == 3


def test_median_and_win_rate():
    rows = [_row("fade", True, x) for x in (-5.0, 1.0, 8.0)]
    g = summarize_short_setups(rows)["groups"][0]
    assert g["median"] == 1.0
    assert abs(g["win"] - (2 / 3) * 100) < 1e-9
