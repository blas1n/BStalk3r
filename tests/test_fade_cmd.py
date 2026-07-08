"""`fade` command over fake grouped + minute data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import src.main as main_mod
from src.config import Settings

T0 = datetime(2026, 7, 2, 13, 30, tzinfo=UTC)


def _bars(rows):  # (high, low, close)
    return [
        {"ts": T0 + timedelta(minutes=i), "open": c, "high": h, "low": low, "close": c}
        for i, (h, low, c) in enumerate(rows)
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
        db_path=str(tmp_path / "f.db"),
        min_day_change_pct=5.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        trailing_stop_pct=0.15,
        max_hold_minutes=180.0,
    )


def test_fade_reports_groups_and_squeeze_tail(tmp_path, capsys):
    today = [
        {"T": "FADE", "c": 9.0, "h": 10.6},  # touched +6% then faded -> fizzle (short wins)
        {"T": "RUN", "c": 15.0, "h": 15.0},  # kept running -> survivor (short squeezed)
    ]
    prev = [{"T": "FADE", "c": 10.0}, {"T": "RUN", "c": 10.0}]
    grouped = _FakeGrouped(today, prev)
    minutes = _FakeMinutes(
        {
            "FADE": _bars([(10.6, 10.6, 10.6), (10.0, 9.5, 9.6), (9.5, 9.4, 9.5)]),  # falls
            "RUN": _bars([(10.6, 10.6, 10.6), (14.0, 10.6, 13.0)]),  # squeezes up
        }
    )
    rc = main_mod.cmd_fade(
        _settings(tmp_path), grouped, minutes, date="2026-07-02", sample=10, throttle_sec=0
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fade/SHORT backtest" in out
    assert "ALL crossers" in out and "squeeze tail" in out
    assert "max adverse up-move" in out
