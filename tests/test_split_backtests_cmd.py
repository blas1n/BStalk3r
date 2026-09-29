"""Every grouped-cache backtest command feeds its engine split-adjusted closes (#47).

One test per call site: a helper-level test says nothing about whether each
command actually routes its panel through the adjustment."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
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
_SPLIT_AT = 30


class _SplitGrouped:
    """SPL does a 10:1 split at bar 30 (raw close / 10); AAA/BBB are ordinary."""

    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            spl = (20.0 + 0.2 * t + (1.0 if t % 3 == 1 else 0.0)) * (0.1 if t >= _SPLIT_AT else 1.0)
            self._by[ds] = [
                {"T": "SPL", "c": round(spl, 4), "v": 5_000_000},
                {"T": "AAA", "c": 20.0 + 0.3 * t + (t % 4), "v": 5_000_000},
                {"T": "BBB", "c": 30.0 - 0.1 * t + (t % 5), "v": 5_000_000},
            ]

    def fetch_grouped(self, d):
        return self._by.get(d, [])


class _NoNews:
    def fetch_day(self, d):
        return []


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "s.db"),
    )


def _max_jump(closes: list[float]) -> float:
    return max(abs(b / a - 1) for a, b in zip(closes, closes[1:], strict=False))


def _spl_from_panel(panel) -> list[float]:
    return [panel[d]["SPL"]["close"] for d in sorted(panel) if "SPL" in panel[d]]


def _spy(monkeypatch, name, grab):
    seen: list[list[float]] = []
    real = getattr(main_mod, name)

    def wrapper(*a, **k):
        got = grab(*a, **k)
        if got:
            seen.append(got)
        return real(*a, **k)

    monkeypatch.setattr(main_mod, name, wrapper)
    return seen


def _mr_grab(dates, closes, dvols, **k):
    # every symbol's series (SPL, AAA, BBB); only SPL can show a split-sized jump
    return list(closes) if len(closes) > _SPLIT_AT else None


_COMMON = dict(min_price=0.1, max_price=1000.0, throttle_sec=0)


def _run(cmd, tmp_path):
    s, g = _settings(tmp_path), _SplitGrouped()
    if cmd == "mrsearch":
        main_mod.cmd_mrsearch(
            s, g, _DATES[0], _DATES[-1], ma_periods=[5], min_dvol_m=0.0, min_n=1, **_COMMON
        )
    elif cmd == "mrportfolio":
        main_mod.cmd_mrportfolio(
            s, g, _DATES[0], _DATES[-1], ma_period=5, min_dvol_m=0.0, **_COMMON
        )
    elif cmd == "xsearch":
        main_mod.cmd_xsearch(s, g, _DATES[0], _DATES[-1], min_dollar_vol_m=0.0, min_n=1, **_COMMON)
    elif cmd == "calsearch":
        main_mod.cmd_calsearch(s, g, _DATES[0], _DATES[-1], min_dvol_m=0.0, **_COMMON)
    else:
        main_mod.cmd_sentsearch(
            s, g, _NoNews(), _DATES[0], _DATES[-1], min_dvol_m=0.0, min_n=1, **_COMMON
        )


_ENGINES = {
    "mrsearch": ("mean_reversion_trades", _mr_grab),
    "mrportfolio": ("mean_reversion_trades", _mr_grab),
    "xsearch": ("cross_sectional_backtest", lambda panel, *a, **k: _spl_from_panel(panel)),
    "calsearch": ("equal_weight_returns", lambda panel, *a, **k: _spl_from_panel(panel)),
    "sentsearch": ("sentiment_backtest", lambda panel, *a, **k: _spl_from_panel(panel)),
}


@pytest.mark.parametrize("cmd", list(_ENGINES))
def test_backtest_command_feeds_split_adjusted_closes(cmd, tmp_path, monkeypatch, capsys):
    name, grab = _ENGINES[cmd]
    seen = _spy(monkeypatch, name, grab)
    _run(cmd, tmp_path)
    assert seen, f"{cmd}: engine never saw SPL"
    spanning = [c for c in seen if len(c) > 1]
    assert spanning
    assert max(_max_jump(c) for c in spanning) < 0.2  # raw would show the -90% day


def test_live_mr_trade_still_reads_raw_closes_during_the_registered_window(monkeypatch):
    """Guard, not an endorsement: live RSI-2/SMA200 consume the same raw grouped
    series. Changing that is a live-rule change and is frozen while the R5
    prospective test runs (docs/preregistration/2026-09-tiebreak-r5.md, to
    2026-12-31). This PR must only touch the backtest commands."""
    import inspect

    src = inspect.getsource(main_mod.cmd_mr_trade)
    assert "split_adjust" not in src
