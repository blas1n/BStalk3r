"""Every grouped-cache backtest command feeds its engine closes adjusted from
corporate-action split events (#47); the live `mr-trade` path is left raw.

One test per call site: a helper-level test says nothing about whether each
command actually routes its panel through the adjustment, or whether `main`
hands each command a split source."""

from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest
import src.main as main_mod
import src.mr_live as mr_live
from src.config import Settings
from src.database import Database
from src.models import MarketSnapshot
from src.splits import SplitEvent


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_DATES = _weekdays(date(2026, 1, 5), 60)
_SPLIT_AT = 30
_SPLIT = SplitEvent("SPL", _DATES[_SPLIT_AT], 1, 10)


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

    def latest_session(self, lag_days=0, _today=None):
        return _DATES[-1]


class _FixtureSplits:
    def __init__(self, events):
        self.events = list(events)
        self.ranges: list[tuple[str, str]] = []

    def fetch_range(self, start, end):
        self.ranges.append((start, end))
        return list(self.events)


class _NoNews:
    def fetch_day(self, d):
        return []


def _settings(tmp_path, **kw):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "s.db"),
        **kw,
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


def _run(cmd, tmp_path, splits):
    s, g = _settings(tmp_path), _SplitGrouped()
    if cmd == "mrsearch":
        main_mod.cmd_mrsearch(
            s,
            g,
            _DATES[0],
            _DATES[-1],
            ma_periods=[5],
            min_dvol_m=0.0,
            min_n=1,
            splits=splits,
            **_COMMON,
        )
    elif cmd == "mrportfolio":
        main_mod.cmd_mrportfolio(
            s, g, _DATES[0], _DATES[-1], ma_period=5, min_dvol_m=0.0, splits=splits, **_COMMON
        )
    elif cmd == "xsearch":
        main_mod.cmd_xsearch(
            s, g, _DATES[0], _DATES[-1], min_dollar_vol_m=0.0, min_n=1, splits=splits, **_COMMON
        )
    elif cmd == "calsearch":
        main_mod.cmd_calsearch(
            s, g, _DATES[0], _DATES[-1], min_dvol_m=0.0, splits=splits, **_COMMON
        )
    else:
        main_mod.cmd_sentsearch(
            s,
            g,
            _NoNews(),
            _DATES[0],
            _DATES[-1],
            min_dvol_m=0.0,
            min_n=1,
            splits=splits,
            **_COMMON,
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
    splits = _FixtureSplits([_SPLIT])
    _run(cmd, tmp_path, splits)
    assert splits.ranges and splits.ranges[0][0] == _DATES[0]  # asked for the window
    assert seen, f"{cmd}: engine never saw SPL"
    assert max(_max_jump(c) for c in seen if len(c) > 1) < 0.2  # raw shows the -90% day
    assert "splits: 1 applied" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", list(_ENGINES))
def test_backtest_command_leaves_a_jump_without_a_split_event_raw(
    cmd, tmp_path, monkeypatch, capsys
):
    """No event, no adjustment: a real spike (CDTX) must reach the engine as-is."""
    name, grab = _ENGINES[cmd]
    seen = _spy(monkeypatch, name, grab)
    _run(cmd, tmp_path, _FixtureSplits([]))
    assert max(_max_jump(c) for c in seen if len(c) > 1) > 0.85
    assert "splits: 0 applied" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", list(_ENGINES))
def test_backtest_command_without_a_split_source_says_so(cmd, tmp_path, capsys):
    _run(cmd, tmp_path, None)
    assert "splits: UNADJUSTED" in capsys.readouterr().out


# --- dispatch: `main` hands each backtest command a Polygon split source -------

_ARGV = {
    "mrsearch": "cmd_mrsearch",
    "mrportfolio": "cmd_mrportfolio",
    "xsearch": "cmd_xsearch",
    "calsearch": "cmd_calsearch",
    "sentsearch": "cmd_sentsearch",
}


class _SourceSpy:
    made: list[tuple[str, str]] = []

    def __init__(self, api_key, cache_path="", **_):
        type(self).made.append((api_key, cache_path))

    def fetch_range(self, start, end):  # pragma: no cover - dispatch stubs the command
        return []


@pytest.mark.parametrize("cmd", list(_ARGV))
def test_main_passes_a_polygon_split_source(cmd, tmp_path, monkeypatch):
    s = _settings(tmp_path, polygon_api_key="PK", splits_cache_path=str(tmp_path / "splits.db"))
    monkeypatch.setattr(main_mod, "load_settings", lambda: s)
    monkeypatch.setattr(main_mod, "PolygonSplitSource", _SourceSpy)
    _SourceSpy.made = []
    got: dict = {}

    def fake_cmd(*a, **k):
        got.update(k)
        return 0

    monkeypatch.setattr(main_mod, _ARGV[cmd], fake_cmd)
    assert main_mod.main([cmd, "--start", "2026-01-05", "--end", "2026-03-27"]) == 0
    assert isinstance(got.get("splits"), _SourceSpy)
    assert _SourceSpy.made == [("PK", str(tmp_path / "splits.db"))]


# --- live path: frozen during the R5 pre-registered window ---------------------


def test_live_mr_trade_still_reads_raw_closes_during_the_registered_window(tmp_path, monkeypatch):
    """Guard, not an endorsement: live RSI-2/SMA200 consume the raw grouped
    series. Changing that is a live-rule change and is frozen while the R5
    prospective test runs (docs/preregistration/2026-09-tiebreak-r5.md, to
    2026-12-31). This PR only touches the backtest commands."""
    assert "splits" not in inspect.signature(main_mod.cmd_mr_trade).parameters
    assert "split_adjust" not in inspect.getsource(main_mod.cmd_mr_trade)
    # #55 bands the backtests on `traded_close`; live bands its raw latest close.
    assert "traded" not in inspect.getsource(main_mod.cmd_mr_trade)
    assert "traded" not in inspect.getsource(mr_live)

    seen: dict = {}
    real = main_mod.mr_decisions

    def spy(closes_by_symbol, *a, **k):
        seen.update(closes_by_symbol)
        return real(closes_by_symbol, *a, **k)

    monkeypatch.setattr(main_mod, "mr_decisions", spy)
    s = _settings(tmp_path, dry_run=True, min_price=0.1, max_price=1000.0)
    db = Database(s.db_path)
    db.init_schema()

    class _Market:
        def get_snapshots(self, symbols):
            return [MarketSnapshot(x, 5.0, -1.0, 1.0, 1.0, 0.1, ask_price=5.0) for x in symbols]

    class _Trading:
        def get_account(self):
            return type("A", (), {"equity": 1e5, "cash": 1e5})()

    main_mod.cmd_mr_trade(
        s,
        _SplitGrouped(),
        _Market(),
        _Trading(),
        db,
        ma_period=40,
        min_price=0.1,
        max_price=1000.0,
        min_dvol_m=0.0,
        throttle_sec=0,
    )
    assert _max_jump(seen["SPL"]) > 0.85  # the raw 10:1 day, unchanged


# --- price band on the as-traded close (#55) ------------------------------------
#
# REV traded at ~$3 until a 1-for-20 reverse split at bar 30 (~$60 after), with
# post-split volume too thin for the dollar-volume floor. Its back-adjusted
# pre-split history (~$60) sits inside the $5-1000 band, so only the as-traded
# close can keep it out: every engine must behave as if REV were absent.
# ABV traded at ~$8 before a 1-for-2 reverse split: in band on both bases.

_REV_SPLIT = SplitEvent("REV", _DATES[_SPLIT_AT], 20, 1)
_ABV_SPLIT = SplitEvent("ABV", _DATES[_SPLIT_AT], 2, 1)


class _BandGrouped:
    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            post = t >= _SPLIT_AT
            step = (0, 1, 2, 1, 0)[t % 5]  # two down days -> RSI-2 = 0
            rev = 3.0 * (1.0 + 0.25 * step) * (20 if post else 1)  # extreme: ranks in the tails
            abv = 8.0 * (1.0 + 0.05 * step) * (2 if post else 1)
            self._by[ds] = [
                {"T": "REV", "c": round(rev, 4), "v": 10 if post else 5_000_000},
                {"T": "ABV", "c": round(abv, 4), "v": 5_000_000},
                {"T": "AAA", "c": 20.0 + 0.3 * t + (t % 4), "v": 5_000_000},
                {"T": "BBB", "c": 30.0 - 0.1 * t + (t % 5), "v": 5_000_000},
                {"T": "CCC", "c": 25.0 + 0.2 * t - (t % 3), "v": 5_000_000},
            ]

    def fetch_grouped(self, d):
        return self._by.get(d, [])


class _BandNews:
    def fetch_day(self, d):
        return [
            {"date": d, "tickers": ["REV"], "score": 0.9},  # REV alone tops the ranking
            {"date": d, "tickers": ["AAA"], "score": 0.8},
            {"date": d, "tickers": ["BBB"], "score": -0.8},
            {"date": d, "tickers": ["ABV", "CCC"], "score": 0.1},
        ]


_BAND = dict(min_price=5.0, max_price=1000.0, throttle_sec=0)


def _run_band(cmd, tmp_path):
    s, g = _settings(tmp_path), _BandGrouped()
    splits = _FixtureSplits([_REV_SPLIT, _ABV_SPLIT])
    if cmd == "mrsearch":
        main_mod.cmd_mrsearch(
            s,
            g,
            _DATES[0],
            _DATES[-1],
            ma_periods=[0],
            entry_rsis=[15.0],
            min_dvol_m=1.0,
            min_n=1,
            splits=splits,
            **_BAND,
        )
    elif cmd == "mrportfolio":
        main_mod.cmd_mrportfolio(
            s, g, _DATES[0], _DATES[-1], ma_period=0, min_dvol_m=1.0, splits=splits, **_BAND
        )
    elif cmd == "xsearch":
        main_mod.cmd_xsearch(
            s, g, _DATES[0], _DATES[-1], min_dollar_vol_m=1.0, min_n=1, splits=splits, **_BAND
        )
    elif cmd == "calsearch":
        main_mod.cmd_calsearch(s, g, _DATES[0], _DATES[-1], min_dvol_m=1.0, splits=splits, **_BAND)
    else:
        main_mod.cmd_sentsearch(
            s,
            g,
            _BandNews(),
            _DATES[0],
            _DATES[-1],
            min_dvol_m=1.0,
            min_n=1,
            min_articles=1,
            splits=splits,
            **_BAND,
        )


def _mr_by_symbol(monkeypatch) -> dict[str, list]:
    """Trades per symbol, keyed by the level of the series' first adjusted close."""
    got: dict[str, list] = {"REV": [], "ABV": []}
    real = main_mod.mean_reversion_trades

    def spy(dates, closes, dvols, **k):
        out = real(dates, closes, dvols, **k)
        if 50 < closes[0] < 70:
            got["REV"].extend(out)
        elif 15 < closes[0] < 17:
            got["ABV"].extend(out)
        return out

    monkeypatch.setattr(main_mod, "mean_reversion_trades", spy)
    return got


@pytest.mark.parametrize("cmd", ["mrsearch", "mrportfolio"])
def test_mr_command_bands_on_the_as_traded_close(cmd, tmp_path, monkeypatch):
    got = _mr_by_symbol(monkeypatch)
    _run_band(cmd, tmp_path)
    assert got["REV"] == [], f"{cmd}: REV entered at an as-traded ~$3"
    assert got["ABV"], f"{cmd}: ABV ($8 as traded, $16 adjusted) must still trade"


_PANEL_ENGINES = {
    "xsearch": "cross_sectional_backtest",
    "calsearch": "equal_weight_returns",
    "sentsearch": "sentiment_backtest",
}


@pytest.mark.parametrize("cmd", list(_PANEL_ENGINES))
def test_panel_command_bands_on_the_as_traded_close(cmd, tmp_path, monkeypatch):
    """Each engine call must return exactly what it returns with REV removed."""
    name = _PANEL_ENGINES[cmd]
    real = getattr(main_mod, name)
    calls: list[tuple[object, object, bool]] = []

    def spy(panel, *a, **k):
        out = real(panel, *a, **k)
        without = {d: {s: r for s, r in day.items() if s != "REV"} for d, day in panel.items()}
        has_rev = any("REV" in day for day in panel.values())
        calls.append((out, real(without, *a, **k), has_rev))
        return out

    monkeypatch.setattr(main_mod, name, spy)
    _run_band(cmd, tmp_path)
    assert any(has_rev for _, _, has_rev in calls), f"{cmd}: REV never reached the engine"
    assert all(out == ref for out, ref, _ in calls), f"{cmd}: REV's adjusted $60 entered the band"
