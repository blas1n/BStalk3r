"""`mrsearch` command over a synthetic oscillating liquid universe."""

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


_DATES = _weekdays(date(2026, 1, 5), 60)


class _FakeGrouped:
    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            rows = []
            for i, s in enumerate(["AAA", "BBB", "CCC"]):
                # rising trend + oscillation -> dips-in-uptrend for RSI-2 to buy
                price = 20.0 + 0.4 * t + 3.0 * math.sin(t * 1.5 + i)
                rows.append({"T": s, "c": round(price, 2), "v": 3_000_000})
            self._by[ds] = rows

    def fetch_grouped(self, d):
        return self._by.get(d, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "m.db"),
    )


def test_mrsearch_runs_split_sweep_and_holdout(tmp_path, capsys):
    rc = main_mod.cmd_mrsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[-1],
        rsi_periods=[2],
        entry_rsis=[10.0],
        exit_rsis=[50.0],
        ma_periods=[5],  # small MA so a 60-session window can warm up
        max_holds=[3, 5],
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        min_n=1,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mrsearch" in out
    assert "HOLDOUT" in out or "no combo cleared" in out


def test_mrsearch_bails_on_too_few_sessions(tmp_path, capsys):
    rc = main_mod.cmd_mrsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start=_DATES[0],
        end=_DATES[10],  # ~8 sessions
        ma_periods=[5],
        min_n=1,
        throttle_sec=0,
    )
    assert rc == 0
    assert "widen" in capsys.readouterr().out
