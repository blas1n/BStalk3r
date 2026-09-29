"""Ranked entry: take the strongest (most-oversold) signals when the book is
full, instead of first-come. Refines the conservative floor from #34."""

from __future__ import annotations

import hashlib
import inspect
from datetime import date, timedelta
from pathlib import Path

import src.main as main_mod
from src.config import Settings
from src.mean_reversion import mean_reversion_trades
from src.mr_live import trailing_avg_dollar_vol
from src.portfolio import mr_rank_key, simulate_portfolio
from src.xsectional import build_panel


def test_trade_records_entry_rsi():
    # strong uptrend, 2-day dip at idx 6-7 (RSI-2 = 0), then rips
    closes = [10, 12, 14, 16, 18, 20, 19, 18.5, 22.0]
    dates = [f"2026-06-{d:02d}" for d in range(1, len(closes) + 1)]
    dvols = [1e8] * len(closes)
    trades = mean_reversion_trades(
        dates,
        [float(c) for c in closes],
        dvols,
        rsi_period=2,
        entry_rsi=10,
        exit_rsi=50,
        ma_period=5,
        max_hold=5,
        min_price=1,
        max_price=1000,
        min_dollar_vol=0,
        cost_frac=0.0,
    )
    assert len(trades) == 1
    assert "entry_rsi" in trades[0]
    assert trades[0]["entry_rsi"] <= 10  # oversold at entry


def test_portfolio_rank_key_admits_strongest_under_capacity():
    price = {
        "WEAK": {"d1": 10.0, "d2": 10.5},  # weaker signal (higher RSI)
        "STRONG": {"d1": 10.0, "d2": 11.0},  # stronger signal (lower RSI)
    }
    # both fire on d1 but only 1 slot; rank_key='entry_rsi' ascending -> STRONG wins
    trades = [
        {"symbol": "WEAK", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 9.0},
        {"symbol": "STRONG", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 1.0},
    ]
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0, rank_key="entry_rsi")
    assert out["stats"]["n_trades"] == 1
    # STRONG (+10%) admitted, not WEAK (+5%) -> d2 return +10%
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9


def test_portfolio_no_rank_key_is_first_come():
    price = {"A": {"d1": 10.0, "d2": 11.0}, "B": {"d1": 10.0, "d2": 12.0}}
    trades = [
        {"symbol": "A", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 5.0},
        {"symbol": "B", "entry_date": "d1", "exit_date": "d2", "entry_rsi": 1.0},
    ]
    # default (no rank) -> first-come admits A (order preserved), B dropped
    out = simulate_portfolio(price, trades, max_positions=1, cost_frac=0.0)
    assert abs(dict(out["daily"])["d2"] - 0.10) < 1e-9  # A's +10%, not B's +20%


# --- #59: `mrportfolio --ranked` ranks on the R5 order ------------------------
#
# `_mr_all_trades` used to drop entry RSI, so `--ranked` sorted every signal on the
# 0.0 default and admitted them first-come. The ranked book must admit same-day
# signals like live `mr_decisions`: RSI asc, then 20-day average dollar volume
# (D-1..D-20, `trailing_avg_dollar_vol`) desc (unknown last), then symbol.


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_D = _weekdays(date(2026, 3, 2), 70)  # mrportfolio needs >= 60 sessions
_DIP = 62  # every name dips into bar _DIP, bounces at _DIP + 1


def _path(tail: tuple[float, float]) -> list[float]:
    """Uptrend (+3/bar), then two changes `tail` into bar _DIP, then +3 bars."""
    c = [10.0 + 3 * i for i in range(_DIP - 1)]
    c.append(c[-1] + tail[0])
    c.append(c[-1] + tail[1])
    while len(c) < len(_D):
        c.append(c[-1] + 3.0)
    return c


# name -> (closes, shares/bar). Input order puts the weakest signal first so a
# first-come book would admit it.
_NAMES = {
    "WEAK": (_path((0.1, -1.0)), 1_000_000),  # RSI-2 = 9.09 at the dip
    "LOWV": (_path((-1.0, -1.0)), 1_000_000),  # RSI-2 = 0, smaller 20d $vol
    "HIGV": (_path((-1.0, -1.0)), 3_000_000),  # RSI-2 = 0, larger 20d $vol
    "TIEB": (_path((-1.0, -1.0)), 3_000_000),  # identical to HIGV -> symbol decides
}


def _grouped_rows(names) -> dict[str, list[dict]]:
    return {
        d: [{"T": s, "c": names[s][0][i], "v": names[s][1]} for s in names]
        for i, d in enumerate(_D)
    }


class _Grouped:
    def __init__(self, names):
        self._by = _grouped_rows(names)

    def fetch_grouped(self, d):
        return self._by.get(d, [])


_KW = dict(
    rsi_period=2,
    entry_rsi=15.0,
    exit_rsi=50.0,
    ma_period=5,
    max_hold=5,
    min_price=1.0,
    max_price=1000.0,
    min_dollar_vol=0.0,
    cost_frac=0.0,
)


def test_mr_all_trades_carries_entry_rsi_and_the_r5_key():
    panel = build_panel(_grouped_rows(_NAMES))
    trades = main_mod._mr_all_trades(main_mod._panel_to_series(panel), _KW)
    dip = {t["symbol"]: t for t in trades if t["entry_date"] == _D[_DIP]}
    assert set(dip) == set(_NAMES)
    assert dip["LOWV"]["entry_rsi"] == 0.0
    assert abs(dip["WEAK"]["entry_rsi"] - 100.0 * 0.1 / 1.1) < 1e-9
    # R5 key: same numbers live computes (D-1..D-20, today excluded)
    live = trailing_avg_dollar_vol(panel, _D[_DIP])
    for s, t in dip.items():
        assert t["rank_dollar_vol"] == live[s]


def test_r5_key_matches_live_when_the_symbol_misses_sessions():
    names = {"AAA": (_path((-1.0, -1.0)), 1_000_000), "BBB": (_path((-1.0, -1.0)), 2_000_000)}
    rows = _grouped_rows(names)
    for d in _D[_DIP - 5 : _DIP - 2]:  # AAA absent 3 sessions inside the window
        rows[d] = [r for r in rows[d] if r["T"] != "AAA"]
    panel = build_panel(rows)
    kw = dict(_KW, ma_period=0)
    trades = main_mod._mr_all_trades(main_mod._panel_to_series(panel), kw)
    for t in trades:
        live = trailing_avg_dollar_vol(panel, t["entry_date"])
        assert t["rank_dollar_vol"] == live.get(t["symbol"])


def test_mr_rank_key_orders_rsi_then_avg_dollar_vol_then_symbol():
    ts = [
        {"symbol": "ZZZ", "entry_rsi": 5.0, "rank_dollar_vol": 1e9},
        {"symbol": "BBB", "entry_rsi": 0.0, "rank_dollar_vol": 1e6},
        {"symbol": "CCC", "entry_rsi": 0.0, "rank_dollar_vol": None},
        {"symbol": "AAA", "entry_rsi": 0.0, "rank_dollar_vol": 1e6},
        {"symbol": "DDD", "entry_rsi": 0.0, "rank_dollar_vol": 5e6},
    ]
    assert [t["symbol"] for t in sorted(ts, key=mr_rank_key)] == [
        "DDD",
        "AAA",
        "BBB",
        "CCC",
        "ZZZ",
    ]


def test_mr_rank_key_refuses_a_trade_without_entry_rsi():
    # the #59 failure mode: a missing RSI silently ranked as 0.0
    try:
        mr_rank_key({"symbol": "AAA", "rank_dollar_vol": 1.0})
    except KeyError:
        return
    raise AssertionError("a trade without entry_rsi must not rank")


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "p.db"),
    )


def _admitted(monkeypatch, tmp_path, names, ranked, slots):
    """Symbols the FULL-window book admits on the dip day."""
    calls: list[list[str]] = []
    real = main_mod.simulate_portfolio

    def spy(*a, **k):
        out = real(*a, **k)
        calls.append([s for d, s in out["taken"] if d == _D[_DIP]])
        return out

    monkeypatch.setattr(main_mod, "simulate_portfolio", spy)
    rc = main_mod.cmd_mrportfolio(
        _settings(tmp_path),
        _Grouped(names),
        start=_D[0],
        end=_D[-1],
        rsi_period=2,
        entry_rsi=15.0,
        exit_rsi=50.0,
        ma_period=5,
        max_hold=5,
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        max_positions=slots,
        cost_bps=0.0,
        ranked=ranked,
        throttle_sec=0,
    )
    assert rc == 0
    return calls[0]  # FULL is the first simulation


def test_ranked_admits_most_oversold_first(monkeypatch, tmp_path):
    names = {k: _NAMES[k] for k in ("WEAK", "LOWV")}
    assert _admitted(monkeypatch, tmp_path, names, ranked=False, slots=1) == ["WEAK"]
    assert _admitted(monkeypatch, tmp_path, names, ranked=True, slots=1) == ["LOWV"]


def test_ranked_ties_on_rsi_follow_the_r5_key_then_symbol(monkeypatch, tmp_path):
    got = _admitted(monkeypatch, tmp_path, _NAMES, ranked=True, slots=3)
    assert got == ["HIGV", "TIEB", "LOWV"]


# --- live rule is frozen during the R5 pre-registered window ------------------
# docs/preregistration/2026-09-tiebreak-r5.md (through 2026-12-31): #59 fixes the
# backtest only. These pins fail on ANY edit to the live path or the simulator.

_ROOT = Path(__file__).resolve().parent.parent
_LIVE_CMD_SHA = "e52649e12e59f8a77b29c2662567086bb7c631f98236b72cc7887078bd03b3f2"
_MR_LIVE_SHA = "06442d1b31fce7edc15808f96939ef9c63a89f9dc1600d8b37f057e6502afafa"
_TIEBREAK_FILES = ("README.md", "bt.py", "core.py", "panel.py", "tiebreak.py")
_TIEBREAK_SHA = "d3a9a4aa3190169386505ce371d5d177bfa9157ac3a471af11b831a74f90865e"


def test_live_mr_trade_source_is_unchanged():
    src = inspect.getsource(main_mod.cmd_mr_trade).encode()
    assert hashlib.sha256(src).hexdigest() == _LIVE_CMD_SHA


def test_mr_live_module_is_unchanged():
    got = hashlib.sha256((_ROOT / "src" / "mr_live.py").read_bytes()).hexdigest()
    assert got == _MR_LIVE_SHA


def test_research_tiebreak_is_unchanged():
    h = hashlib.sha256()
    for name in _TIEBREAK_FILES:
        p = Path("research/tiebreak") / name
        h.update(str(p).encode())
        h.update((_ROOT / p).read_bytes())
    assert h.hexdigest() == _TIEBREAK_SHA
