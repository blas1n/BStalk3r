"""`accumulate-shorts` command — both strategies over fake grouped/minute/Alpaca.

Records the *would-be* short outcome + shortable status for each setup (fade
crosser + exhaustion run-end) into SQLite. No real orders; the target runners are
not shortable anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import src.main as main_mod
from src.config import Settings
from src.database import Database

T0 = datetime(2026, 7, 3, 13, 30, tzinfo=UTC)


def _bars(rows):  # (high, low, close)
    return [
        {
            "ts": T0 + timedelta(minutes=i),
            "open": c,
            "high": h,
            "low": low,
            "close": c,
            "volume": 1000,
        }
        for i, (h, low, c) in enumerate(rows)
    ]


class _FakeGrouped:
    """Date-aware grouped source: fetch_grouped(d) -> that day's rows."""

    def __init__(self, by_date):
        self._by = by_date

    def fetch_grouped(self, date):
        return self._by.get(date, [])

    def prev_session_rows(self, date):
        # the session immediately before `date` in our fixture calendar
        order = sorted(self._by)
        idx = order.index(date) if date in order else len(order)
        return self._by[order[idx - 1]] if idx > 0 else []


class _FakeMinutes:
    def __init__(self, by_symbol):
        self._by = by_symbol

    def fetch(self, symbol, date):
        return self._by.get(symbol, [])


class _FakeShortability:
    def __init__(self, status_by_symbol, default=None):
        self._by = status_by_symbol
        self._default = default or {"tradable": True, "shortable": False, "easy_to_borrow": False}

    def get_shortability(self, symbol):
        return self._by.get(symbol, self._default)


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "s.db"),
        min_price=1.0,
        max_price=50.0,
        min_day_change_pct=5.0,
        stop_loss_pct=0.15,
        take_profit_pct=0.10,
        trailing_stop_pct=0.20,
        max_hold_minutes=180.0,
    )


def test_accumulate_shorts_records_both_strategies(tmp_path, capsys):
    by_date = {
        "2026-07-01": [{"T": "EXH", "o": 10, "h": 10, "l": 10, "c": 10.0}],
        "2026-07-02": [
            {"T": "EXH", "o": 15, "h": 15, "l": 15, "c": 15.0},  # +50% run over 1 session
            {"T": "FAD", "o": 10, "h": 10, "l": 10, "c": 10.0},  # prev close for the crosser
        ],
        "2026-07-03": [  # target session (the short day)
            {"T": "EXH", "o": 15, "h": 15.2, "l": 12, "c": 12.5},  # exhaustion short day
            {"T": "FAD", "o": 10, "h": 10.6, "l": 9, "c": 9.0},  # +6% crosser that fizzles
        ],
    }
    grouped = _FakeGrouped(by_date)
    minutes = _FakeMinutes(
        {
            # FAD breakout: crosses +5% over prev_close 10 (->10.6) then fades
            "FAD": _bars([(10.0, 10.0, 10.0), (10.6, 10.6, 10.6), (9.5, 9.4, 9.5)]),
            # EXH breakdown: opens 15.2, breaks down through 14.7 (-2% of 15) then fades
            "EXH": _bars([(15.2, 15.2, 15.2), (14.6, 14.6, 14.6), (13.0, 12.9, 13.0)]),
        }
    )
    short = _FakeShortability(
        {
            "FAD": {"tradable": True, "shortable": False, "easy_to_borrow": False},
            "EXH": {"tradable": True, "shortable": False, "easy_to_borrow": False},
        }
    )
    db = Database(_settings(tmp_path).db_path)
    db.init_schema()

    rc = main_mod.cmd_accumulate_shorts(
        _settings(tmp_path),
        grouped,
        minutes,
        short,
        db,
        date="2026-07-03",
        run_days=1,
        run_gain=40.0,
        exh_trigger=2.0,
        throttle_sec=0,
    )
    assert rc == 0
    assert db.count_short_setups() == 2
    strategies = {r["strategy"] for r in db.get_short_setups(session_date="2026-07-03")}
    assert strategies == {"fade", "exhaustion"}
    out = capsys.readouterr().out
    assert "accumulate-shorts" in out.lower() or "short setups" in out.lower()
    assert "shortable" in out.lower()


def test_exhaustion_not_starved_by_fade_sample_cap(tmp_path):
    # Many fade crossers + one exhaustion run-end, with a tiny sample budget.
    # Exhaustion (rare) must survive the cap, not be crowded out by fade volume.
    # prev session (07-02) carries the run-end + the crossers' prior closes
    prev = [{"T": f"F{i}", "o": 10, "h": 10, "l": 10, "c": 10.0} for i in range(5)]
    prev.append({"T": "EXH", "o": 15, "h": 15, "l": 15, "c": 15.0})  # +50% run end
    today = [{"T": f"F{i}", "o": 10, "h": 10.6, "l": 9, "c": 9.0} for i in range(5)]
    today.append({"T": "EXH", "o": 15, "h": 15.2, "l": 12, "c": 12.5})
    by_date = {
        "2026-07-01": [{"T": "EXH", "o": 10, "h": 10, "l": 10, "c": 10.0}],  # run start
        "2026-07-02": prev,
        "2026-07-03": today,
    }
    grouped = _FakeGrouped(by_date)
    fade_bar = _bars([(10.0, 10.0, 10.0), (10.6, 10.6, 10.6), (9.5, 9.4, 9.5)])
    exh_bar = _bars([(15.2, 15.2, 15.2), (14.6, 14.6, 14.6), (13.0, 12.9, 13.0)])
    minutes = _FakeMinutes({**{f"F{i}": fade_bar for i in range(5)}, "EXH": exh_bar})
    s = _settings(tmp_path)
    db = Database(s.db_path)
    db.init_schema()
    main_mod.cmd_accumulate_shorts(
        s,
        grouped,
        minutes,
        _FakeShortability({}),
        db,
        date="2026-07-03",
        run_days=1,
        run_gain=40.0,
        sample=2,
        throttle_sec=0,
    )
    strategies = {r["strategy"] for r in db.get_short_setups(session_date="2026-07-03")}
    assert "exhaustion" in strategies  # survived the cap despite 5 fade crossers


def test_accumulate_shorts_is_idempotent(tmp_path):
    by_date = {
        "2026-07-02": [{"T": "FAD", "o": 10, "h": 10, "l": 10, "c": 10.0}],
        "2026-07-03": [{"T": "FAD", "o": 10, "h": 10.6, "l": 9, "c": 9.0}],
    }
    grouped = _FakeGrouped(by_date)
    minutes = _FakeMinutes(
        {"FAD": _bars([(10.0, 10.0, 10.0), (10.6, 10.6, 10.6), (9.5, 9.4, 9.5)])}
    )
    short = _FakeShortability({})
    s = _settings(tmp_path)
    db = Database(s.db_path)
    db.init_schema()
    for _ in range(2):
        main_mod.cmd_accumulate_shorts(
            s, grouped, minutes, short, db, date="2026-07-03", run_days=1, throttle_sec=0
        )
    assert db.count_short_setups() == 1  # re-run upserts, no duplicates
