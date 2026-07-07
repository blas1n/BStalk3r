"""`crosser` command: survivorship-inclusive backtest over fake grouped+minute data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import src.main as main_mod
from src.config import Settings

T0 = datetime(2026, 7, 2, 13, 30, tzinfo=UTC)  # 09:30 ET


def _bars(closes):
    return [
        {"ts": T0 + timedelta(minutes=i), "open": c, "high": c, "low": c, "close": c}
        for i, c in enumerate(closes)
    ]


class _FakeGrouped:
    def __init__(self, today, prev):
        self._t, self._p = today, prev

    def fetch_grouped(self, date):
        return self._t

    def prev_session_rows(self, date):
        return self._p


class _FakeMinutes:
    def __init__(self, by_symbol):
        self._by = by_symbol

    def fetch(self, symbol, date):
        return self._by.get(symbol, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "c.db"),
        min_day_change_pct=5.0,
        take_profit_pct=0.10,
        max_hold_minutes=180.0,
        trailing_stop_pct=0.15,
    )


def test_crosser_reports_all_survivors_fizzles(tmp_path, capsys):
    # WINN closed +10% (survivor); FIZZ touched +6% then closed -10% (fizzle)
    today = [
        {"T": "WINN", "c": 11.0, "h": 12.3},
        {"T": "FIZZ", "c": 9.0, "h": 10.6},
    ]
    prev = [{"T": "WINN", "c": 10.0}, {"T": "FIZZ", "c": 10.0}]
    grouped = _FakeGrouped(today, prev)
    minutes = _FakeMinutes(
        {
            "WINN": _bars([10.6, 11.0, 12.3, 12.3]),  # rides to +10% take-profit
            "FIZZ": _bars([10.6, 10.0, 10.0]),  # stops out
        }
    )

    rc = main_mod.cmd_crosser(
        _settings(tmp_path), grouped, minutes, date="2026-07-02", sample=10, throttle_sec=0
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 intraday +5% crossers (50% fizzles)" in out
    assert "ALL crossers" in out and "survivors" in out and "fizzles" in out


def test_crosser_no_data(tmp_path, capsys):
    grouped = _FakeGrouped([], [])
    rc = main_mod.cmd_crosser(
        _settings(tmp_path), grouped, _FakeMinutes({}), date="2026-07-02", throttle_sec=0
    )
    assert rc == 1
    assert "No grouped data" in capsys.readouterr().out
