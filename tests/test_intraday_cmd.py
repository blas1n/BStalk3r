"""`intraday` command over seeded screened rows + a fake minute-bars provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import src.main as main_mod
from src.config import Settings
from src.database import Database
from src.models import MarketSnapshot

T0 = datetime(2026, 6, 9, 13, 30, tzinfo=UTC)  # 09:30 ET


def _bars(closes):
    return [
        {"ts": T0 + timedelta(minutes=i), "open": c, "high": c, "low": c, "close": c}
        for i, c in enumerate(closes)
    ]


class _FakeMinutes:
    def __init__(self, by_symbol):
        self._by = by_symbol
        self.calls = []

    def fetch(self, symbol, date):
        self.calls.append((symbol, date))
        return self._by.get(symbol, [])


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "i.db"),
    )


def _seed(db, symbol, session, last_price, chg):
    ts = datetime.fromisoformat(session + "T16:00:00+00:00")
    db.record_screened(
        [MarketSnapshot(symbol, last_price, chg, 12.0, 1.0, 0.0, timestamp=ts)], source="polygon"
    )


def test_intraday_runs_and_reports(tmp_path, capsys):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    # runner closed 12.0 after +20% -> prev_close 10.0; entry at +5% (>=10.5)
    _seed(db, "WIN", "2026-06-09", 12.0, 20.0)
    # bars: enter at 10.6, run to +16% (take-profit), pad a trailing bar
    provider = _FakeMinutes({"WIN": _bars([10.6, 11.0, 12.3, 12.3])})

    rc = main_mod.cmd_intraday(settings, db, provider, limit=10, sweep_hold=[30.0], throttle_sec=0)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Intraday hit-and-run over 1/1 runner(s)" in out
    assert "take_profit_scale" in out  # exit reason surfaced
    assert provider.calls == [("WIN", "2026-06-09")]


def test_intraday_grid_fetches_bars_once_per_runner(tmp_path, capsys):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed(db, "WIN", "2026-06-09", 12.0, 20.0)
    provider = _FakeMinutes({"WIN": _bars([10.6, 11.0, 12.3, 12.3])})

    # 2 entry x 2 take-profit x 2 hold = 8 grid cells, but ONE fetch
    rc = main_mod.cmd_intraday(
        settings,
        db,
        provider,
        limit=10,
        sweep_entry=[3.0, 5.0],
        sweep_tp=[8.0, 15.0],
        sweep_hold=[15.0, 30.0],
        throttle_sec=0,
    )
    assert rc == 0
    assert provider.calls == [("WIN", "2026-06-09")]  # fetched once despite 8 variants
    out = capsys.readouterr().out
    assert "e3 tp8 h15" in out and "e5 tp15 h30" in out  # labeled grid rows


def test_intraday_skips_runner_without_minute_data(tmp_path, capsys):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed(db, "NODATA", "2026-06-09", 12.0, 20.0)
    provider = _FakeMinutes({})  # no bars -> 403/empty

    rc = main_mod.cmd_intraday(settings, db, provider, limit=10, sweep_hold=[30.0], throttle_sec=0)
    assert rc == 0
    assert "over 0/1 runner(s)" in capsys.readouterr().out


def test_intraday_band_excludes_out_of_range_price(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed(db, "CHEAP", "2026-06-09", 0.5, 20.0)  # below $1
    _seed(db, "RICH", "2026-06-09", 80.0, 20.0)  # above $50
    band = db.get_screened_band(settings.min_price, settings.max_price)
    assert {r["symbol"] for r in band} == set()  # both outside [1,50]
