"""`sentsearch` command over synthetic prices + news."""

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


_DATES = _weekdays(date(2026, 1, 5), 40)
_SYMS = ["AAA", "BBB", "CCC", "DDD"]


class _FakeGrouped:
    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            rows = [
                {"T": s, "c": round(20 + 0.3 * t + i, 2), "v": 5_000_000}
                for i, s in enumerate(_SYMS)
            ]
            self._by[ds] = rows

    def fetch_grouped(self, d):
        return self._by.get(d, [])


class _FakeNews:
    def fetch_day(self, d):
        # alternate positive/negative headlines across the universe each day
        return [
            {"date": d, "tickers": ["AAA"], "score": 0.8},
            {"date": d, "tickers": ["DDD"], "score": -0.8},
        ]


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "s.db"),
    )


def test_sentsearch_runs_split_sweep_holdout(tmp_path, capsys):
    rc = main_mod.cmd_sentsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        _FakeNews(),
        start=_DATES[0],
        end=_DATES[-1],
        formations=[1],
        holds=[1],
        quantiles=[0.25],
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        min_articles=1,
        min_n=1,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "sentsearch" in out
    assert "HOLDOUT" in out or "no combo cleared" in out


def test_sentsearch_bails_on_too_few_sessions(tmp_path, capsys):
    rc = main_mod.cmd_sentsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        _FakeNews(),
        start=_DATES[0],
        end=_DATES[5],
    )
    assert rc == 0
    assert "widen" in capsys.readouterr().out
