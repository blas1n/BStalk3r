"""`mrportfolio` command over a synthetic oscillating universe."""

from __future__ import annotations

import math
from datetime import date, timedelta

import src.main as main_mod
from src.config import Settings


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_DATES = _weekdays(date(2026, 1, 5), 80)


class _FakeGrouped:
    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            rows = [
                {"T": s, "c": round(20 + 0.4 * t + 3 * math.sin(t * 1.5 + i), 2), "v": 5_000_000}
                for i, s in enumerate(["AAA", "BBB", "CCC"])
            ]
            self._by[ds] = rows

    def fetch_grouped(self, d):
        return self._by.get(d, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "p.db"),
    )


def test_mrportfolio_reports_account_curve_and_halves(tmp_path, capsys):
    rc = main_mod.cmd_mrportfolio(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[-1],
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        max_positions=3,
        cost_bps=10,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mrportfolio" in out
    assert "FULL" in out and "Sharpe" in out and "capacity" in out
    assert "2024-H" in out and "2025-26H" in out


def test_mrportfolio_bails_on_too_few_sessions(tmp_path, capsys):
    rc = main_mod.cmd_mrportfolio(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[10],
        ma_period=5,
        throttle_sec=0,
    )
    assert rc == 0
    assert "widen" in capsys.readouterr().out
