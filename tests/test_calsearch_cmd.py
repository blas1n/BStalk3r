"""`calsearch` command over a synthetic universe with a turn-of-month bump."""

from __future__ import annotations

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


_DATES = _weekdays(date(2026, 1, 1), 90)  # ~4.3 months


class _FakeGrouped:
    def __init__(self):
        # baseline drift + an extra bump on the 1st trading day of each month
        self._by = {}
        prev_month = None
        price = {"AAA": 20.0, "BBB": 30.0}
        for ds in _DATES:
            month = ds[:7]
            bump = 1.03 if month != prev_month else 1.001  # month-start pop
            prev_month = month
            rows = []
            for s in ("AAA", "BBB"):
                price[s] *= bump
                rows.append({"T": s, "c": round(price[s], 2), "v": 5_000_000})
            self._by[ds] = rows

    def fetch_grouped(self, d):
        return self._by.get(d, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "c.db"),
    )


def test_calsearch_runs_and_finds_tom_concentration(tmp_path, capsys):
    rc = main_mod.cmd_calsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[-1],
        days_befores=[1],
        days_afters=[1, 2],
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "calsearch" in out
    assert "concentration" in out
    assert "HOLDOUT" in out or "no window" in out


def test_calsearch_bails_on_too_few_sessions(tmp_path, capsys):
    rc = main_mod.cmd_calsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[10],
        throttle_sec=0,
    )
    assert rc == 0
    assert "widen" in capsys.readouterr().out
