"""Every grouped-cache backtest command splits a reused ticker's series at the
identity break (#58); a halt gap without an identity change stays one series.
One test per call site, plus `main` handing each command a Polygon source.

Fixture: RUS and HLT print the same prices — an uptrend, a two-day dip into
bar 39 (RSI-2 = 0, an entry), no bars 40..49, then a lower-priced uptrend. For
RUS a different company took the ticker at bar 50 (Polygon ticker event); HLT
was merely halted. AAA trades every session and must never be looked up."""

from __future__ import annotations

import hashlib
import inspect
from datetime import date, timedelta
from pathlib import Path

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


_DATES = _weekdays(date(2026, 1, 5), 80)
_LAST_OLD, _FIRST_NEW = 39, 50
_GAP = range(_LAST_OLD + 1, _FIRST_NEW)


def _path(t: int) -> float:
    if t < 38:
        return 20.0 + 2.0 * t
    if t == 38:
        return 20.0 + 2.0 * 37 - 0.5
    if t == 39:
        return 20.0 + 2.0 * 37 - 1.0
    return 60.0 + 0.5 * (t - _FIRST_NEW) + (1.0 if t % 3 == 0 else 0.0)


class _Grouped:
    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            rows = [{"T": "AAA", "c": 20.0 + 0.3 * t + (t % 4), "v": 5_000_000}]
            if t not in _GAP:
                rows += [{"T": s, "c": _path(t), "v": 5_000_000} for s in ("RUS", "HLT")]
            self._by[ds] = rows

    def fetch_grouped(self, d):
        return self._by.get(d, [])

    def latest_session(self, lag_days=0, _today=None):
        return _DATES[-1]


class _Identity:
    def __init__(self, table):
        self.table = table
        self.asked: list[tuple[str, str]] = []

    def acquisitions(self, ticker, known_after):
        self.asked.append((ticker, known_after))
        return list(self.table.get(ticker, []))


def _reused():
    # RUS: current holder took the ticker at bar 50; HLT: listed long ago
    return _Identity({"RUS": [_DATES[_FIRST_NEW]], "HLT": ["2001-05-01"]})


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


_COMMON = dict(min_price=0.1, max_price=1000.0, throttle_sec=0)


def _run(cmd, tmp_path, ident):
    s, g = _settings(tmp_path), _Grouped()
    a, b = _DATES[0], _DATES[-1]
    if cmd == "mrsearch":
        main_mod.cmd_mrsearch(
            s, g, a, b, ma_periods=[5], min_dvol_m=0.0, min_n=1, identity=ident, **_COMMON
        )
    elif cmd == "mrportfolio":
        main_mod.cmd_mrportfolio(s, g, a, b, ma_period=5, min_dvol_m=0.0, identity=ident, **_COMMON)
    elif cmd == "xsearch":
        main_mod.cmd_xsearch(s, g, a, b, min_dollar_vol_m=0.0, min_n=1, identity=ident, **_COMMON)
    elif cmd == "calsearch":
        main_mod.cmd_calsearch(s, g, a, b, min_dvol_m=0.0, identity=ident, **_COMMON)
    else:
        main_mod.cmd_sentsearch(
            s, g, _NoNews(), a, b, min_dvol_m=0.0, min_n=1, identity=ident, **_COMMON
        )


# --- per-symbol engines: no trade spans the reuse gap -----------------------------


def _spy_mr(monkeypatch):
    """(series dates, trades) for every per-symbol walk the command makes."""
    seen: list[tuple[list[str], list[dict]]] = []
    real = main_mod.mean_reversion_trades

    def wrapper(dates, *a, **k):
        out = real(dates, *a, **k)
        seen.append((list(dates), out))
        return out

    monkeypatch.setattr(main_mod, "mean_reversion_trades", wrapper)
    return seen


def _crossing(seen):
    """Walks whose series holds bars on both sides of the gap, and all trades
    that enter before it and exit after it."""
    lo, hi = _DATES[_LAST_OLD], _DATES[_FIRST_NEW]
    gap = _DATES[_GAP[0]]
    walks = [ds for ds, _ in seen if lo in ds and hi in ds and gap not in ds]  # not AAA
    spans = [t for _, ts in seen for t in ts if t["entry_date"] <= lo and t["exit_date"] >= hi]
    return walks, spans


@pytest.mark.parametrize("cmd", ["mrsearch", "mrportfolio"])
def test_reused_ticker_produces_no_trade_spanning_the_gap(cmd, tmp_path, monkeypatch, capsys):
    seen = _spy_mr(monkeypatch)
    _run(cmd, tmp_path, None)
    raw_walks, raw_spans = _crossing(seen)
    seen.clear()
    ident = _reused()
    _run(cmd, tmp_path, ident)
    walks, spans = _crossing(seen)
    # unchecked: RUS and HLT both cross; checked: only the HLT halt does
    assert len(raw_walks) == 2 * len(walks) > 0
    assert len(raw_spans) == 2 * len(spans) > 0
    assert "identity: 1 reused ticker split" in capsys.readouterr().out
    assert sorted(t for t, _ in ident.asked) == ["HLT", "RUS"]  # AAA never looked up


def test_mrportfolio_books_no_rus_trade_across_the_gap(tmp_path, monkeypatch):
    got: list[dict] = []
    real = main_mod._mr_all_trades

    def spy(*a, **k):
        out = real(*a, **k)
        got.extend(out)
        return out

    monkeypatch.setattr(main_mod, "_mr_all_trades", spy)
    _run("mrportfolio", tmp_path, _reused())
    across = {
        t["symbol"]
        for t in got
        if t["entry_date"] <= _DATES[_LAST_OLD] and t["exit_date"] >= _DATES[_FIRST_NEW]
    }
    assert across == {"HLT"}  # the halt is not split; the reuse is


def test_without_an_identity_source_the_fixture_does_span_the_gap(tmp_path, monkeypatch, capsys):
    """Control for the test above: unchecked, RUS books the fake cross-company trade."""
    got: list[dict] = []
    real = main_mod._mr_all_trades

    def spy(*a, **k):
        out = real(*a, **k)
        got.extend(out)
        return out

    monkeypatch.setattr(main_mod, "_mr_all_trades", spy)
    _run("mrportfolio", tmp_path, None)
    across = {
        t["symbol"]
        for t in got
        if t["entry_date"] <= _DATES[_LAST_OLD] and t["exit_date"] >= _DATES[_FIRST_NEW]
    }
    assert across == {"HLT", "RUS"}
    assert "identity: UNCHECKED" in capsys.readouterr().out


# --- panel engines: the earlier company is a different symbol ---------------------


def _panel_grab(panel, *a, **k):
    return {(s, d) for d, day in panel.items() for s in day if s != "AAA"}


_PANEL_ENGINES = {
    "xsearch": "cross_sectional_backtest",
    "calsearch": "equal_weight_returns",
    "sentsearch": "sentiment_backtest",
}


@pytest.mark.parametrize("cmd", list(_PANEL_ENGINES))
def test_panel_engines_see_the_earlier_company_under_its_own_name(
    cmd, tmp_path, monkeypatch, capsys
):
    seen: set[tuple[str, str]] = set()
    name = _PANEL_ENGINES[cmd]
    real = getattr(main_mod, name)

    def wrapper(panel, *a, **k):
        seen.update(_panel_grab(panel))
        return real(panel, *a, **k)

    monkeypatch.setattr(main_mod, name, wrapper)
    _run(cmd, tmp_path, _reused())
    old = f"RUS<{_DATES[_FIRST_NEW]}"
    rus = {d for s, d in seen if s == "RUS"}
    pre = {d for s, d in seen if s == old}
    # sentsearch never reaches its test split without news, so RUS may be absent
    assert all(d >= _DATES[_FIRST_NEW] for d in rus)
    assert pre and max(pre) <= _DATES[_LAST_OLD]
    assert {s for s, _ in seen} <= {"RUS", old, "HLT"}  # the halt is never renamed
    assert min(d for s, d in seen if s == "HLT") <= _DATES[_LAST_OLD]
    assert "identity: 1 reused ticker split" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["mrsearch", "mrportfolio", *_PANEL_ENGINES])
def test_backtest_command_without_an_identity_source_says_so(cmd, tmp_path, capsys):
    _run(cmd, tmp_path, None)
    assert "identity: UNCHECKED" in capsys.readouterr().out


# --- dispatch: `main` hands each backtest command a Polygon identity source ------

_ARGV = {
    "mrsearch": "cmd_mrsearch",
    "mrportfolio": "cmd_mrportfolio",
    "xsearch": "cmd_xsearch",
    "calsearch": "cmd_calsearch",
    "sentsearch": "cmd_sentsearch",
}


class _SourceSpy:
    made: list[tuple[str, str, int]] = []

    def __init__(self, api_key, cache_path="", throttle_sec=0, **_):
        type(self).made.append((api_key, cache_path, throttle_sec))

    def acquisitions(self, ticker, known_after):  # pragma: no cover - command is stubbed
        return []


@pytest.mark.parametrize("cmd", list(_ARGV))
def test_main_passes_a_polygon_identity_source(cmd, tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        polygon_api_key="PK",
        identity_cache_path=str(tmp_path / "identity.db"),
        outcome_throttle_sec=13,
    )
    monkeypatch.setattr(main_mod, "load_settings", lambda: s)
    monkeypatch.setattr(main_mod, "PolygonIdentitySource", _SourceSpy)
    _SourceSpy.made = []
    got: dict = {}

    def fake_cmd(*a, **k):
        got.update(k)
        return 0

    monkeypatch.setattr(main_mod, _ARGV[cmd], fake_cmd)
    assert main_mod.main([cmd, "--start", "2026-01-05", "--end", "2026-03-27"]) == 0
    assert isinstance(got.get("identity"), _SourceSpy)
    assert _SourceSpy.made == [("PK", str(tmp_path / "identity.db"), 13)]


# --- live path: frozen during the R5 pre-registered window ---------------------
# docs/preregistration/2026-09-tiebreak-r5.md (through 2026-12-31). Live builds its
# RSI-2/SMA200 series by ticker too (#58 exposure) — recorded, fixed after the
# window. These pins fail on ANY edit to the live path or the R5 simulator.

_ROOT = Path(__file__).resolve().parent.parent
_LIVE_CMD_SHA = "e52649e12e59f8a77b29c2662567086bb7c631f98236b72cc7887078bd03b3f2"
_MR_LIVE_SHA = "06442d1b31fce7edc15808f96939ef9c63a89f9dc1600d8b37f057e6502afafa"
_TIEBREAK_FILES = ("README.md", "bt.py", "core.py", "panel.py", "tiebreak.py")
_TIEBREAK_SHA = "d3a9a4aa3190169386505ce371d5d177bfa9157ac3a471af11b831a74f90865e"


def test_live_mr_trade_takes_no_identity_source_and_is_unchanged():
    assert "identity" not in inspect.signature(main_mod.cmd_mr_trade).parameters
    src = inspect.getsource(main_mod.cmd_mr_trade).encode()
    assert hashlib.sha256(src).hexdigest() == _LIVE_CMD_SHA


def test_live_modules_are_unchanged():
    got = hashlib.sha256((_ROOT / "src" / "mr_live.py").read_bytes()).hexdigest()
    assert got == _MR_LIVE_SHA
    h = hashlib.sha256()
    for name in _TIEBREAK_FILES:
        p = Path("research/tiebreak") / name
        h.update(str(p).encode())
        h.update((_ROOT / p).read_bytes())
    assert h.hexdigest() == _TIEBREAK_SHA
