"""`xsearch` command over a synthetic liquid daily universe."""

from __future__ import annotations

import math

import src.main as main_mod
from src.config import Settings

# 20 weekday sessions (2026-06-01 Mon .. 2026-06-26 Fri)
_DATES = [
    f"2026-06-{d:02d}"
    for d in list(range(1, 6)) + list(range(8, 13)) + list(range(15, 20)) + list(range(22, 27))
]
_SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


class _FakeGrouped:
    def __init__(self):
        # mean-reverting zig-zag prices so a reversal signal has something to find
        self._by = {}
        for t, date in enumerate(_DATES):
            rows = []
            for i, s in enumerate(_SYMS):
                price = 20.0 + 5.0 * math.sin(t + i)
                rows.append({"T": s, "c": round(price, 2), "v": 2_000_000})
            self._by[date] = rows

    def fetch_grouped(self, date):
        return self._by.get(date, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "x.db"),
    )


def test_xsearch_runs_split_sweep_and_holdout(tmp_path, capsys):
    rc = main_mod.cmd_xsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start="2026-06-01",
        end="2026-06-26",
        strategies=["reversal", "momentum"],
        formations=[1, 2],
        holds=[1],
        quantiles=[0.33],
        min_price=1.0,
        max_price=1000.0,
        min_dollar_vol_m=0.0,
        min_n=2,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "xsearch" in out
    assert "HOLDOUT" in out
    assert "reversal" in out or "momentum" in out


def test_xsearch_bails_on_too_few_sessions(tmp_path, capsys):
    rc = main_mod.cmd_xsearch(
        _settings(tmp_path),
        _FakeGrouped(),
        start="2026-06-01",
        end="2026-06-03",  # only ~3 sessions
        min_n=2,
        throttle_sec=0,
    )
    assert rc == 0
    assert "widen the window" in capsys.readouterr().out
