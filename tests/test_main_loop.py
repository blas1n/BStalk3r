"""Loop wiring: scan + entry + exit, with the data boundary faked.

dry_run=True so no broker order is ever attempted; the fake trading client only
serves account equity. This guards that the realtime tick actually opens and
closes positions end-to-end (the glue, not just the pure rules).
"""

from __future__ import annotations

from dataclasses import dataclass

import src.main as main_mod
from src.config import Settings
from src.database import Database
from src.execution import ExecutionEngine

from tests.conftest import make_snapshot


class _FakeSource:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def fetch(self):
        return list(self._snapshots)


class _FakeMarket:
    """Per-symbol lookup for held positions that dropped off the screen."""

    def __init__(self, snapshots):
        self._by = {s.symbol: s for s in snapshots}

    def get_snapshots(self, symbols):
        return [self._by[s] for s in symbols if s in self._by]


@dataclass
class _Acct:
    equity: str = "100000"


class _FakeTrading:
    def get_account(self):
        return _Acct()


def _settings(tmp_path, **over):
    base = dict(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        dry_run=True,
        db_path=str(tmp_path / "loop.db"),
        universe="RUNR",
    )
    base.update(over)
    return Settings(**base)


def _engine(settings, db):
    return ExecutionEngine(_FakeTrading(), db, settings.build_exec_params(), settings.dry_run, None)


def _run_tick(settings, db, snapshots, monkeypatch, near_close=False, market_snapshots=None):
    monkeypatch.setattr(main_mod, "_near_market_close", lambda *a, **k: near_close)
    main_mod._tick(
        settings,
        _FakeSource(snapshots),
        _FakeMarket(market_snapshots if market_snapshots is not None else snapshots),
        db,
        _engine(settings, db),
        _FakeTrading(),
        settings.build_entry_params(),
        settings.build_exit_params(),
        settings.build_risk_params(),
        settings.build_scan_filters(),
        {},
    )


def test_tick_opens_position_for_clean_runner(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()

    _run_tick(settings, db, [make_snapshot(symbol="RUNR")], monkeypatch)

    opens = db.get_open_positions()
    assert [p["symbol"] for p in opens] == ["RUNR"]
    assert db.count_signals() == 1
    # dry_run -> order logged as dry_run, no broker hit
    assert db.get_order(1)["status"] == "dry_run"


def test_tick_exits_open_position_on_stop_loss(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    from datetime import UTC, datetime

    db.insert_position("RUNR", datetime.now(UTC), 10.0, 100)

    _run_tick(settings, db, [make_snapshot(symbol="RUNR", last_price=9.0)], monkeypatch)

    closed = db.get_closed_positions()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"
    assert db.get_open_positions() == []


def test_held_symbol_off_screen_still_exits(tmp_path, monkeypatch):
    """Position no longer in the screened set is priced via the market lookup."""
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    from datetime import UTC, datetime

    db.insert_position("GONE", datetime.now(UTC), 10.0, 100)
    crashed = make_snapshot(symbol="GONE", last_price=9.0)

    # source returns nothing for GONE; market lookup provides the crashed price
    _run_tick(settings, db, [], monkeypatch, market_snapshots=[crashed])

    closed = db.get_closed_positions()
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop_loss"


def test_scan_command_logs_and_returns_zero(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    source = _FakeSource([make_snapshot(symbol="RUNR"), make_snapshot(symbol="DUD", rvol=1.0)])

    rc = main_mod.cmd_scan(settings, source, db)
    assert rc == 0
    assert db.count_signals() == 1  # only the passing candidate logged


def test_unhealthy_data_blocks_entry(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()

    _run_tick(settings, db, [], monkeypatch)  # no snapshots -> unhealthy
    assert db.get_open_positions() == []


def test_build_source_polygon_grouped_is_default(tmp_path):
    settings = _settings(tmp_path, universe_source="polygon", polygon_api_key="key")
    src = main_mod._build_source(settings, market=None)
    from src.sources import PolygonGroupedSource

    assert isinstance(src, PolygonGroupedSource)


def test_build_source_polygon_intraday_uses_gainers(tmp_path):
    settings = _settings(
        tmp_path, universe_source="polygon", polygon_api_key="key", polygon_intraday=True
    )
    src = main_mod._build_source(settings, market=None)
    from src.sources import PolygonGainersSource

    assert isinstance(src, PolygonGainersSource)


def test_build_source_polygon_without_key_errors(tmp_path):
    import pytest

    settings = _settings(tmp_path, universe_source="polygon", polygon_api_key="")
    with pytest.raises(RuntimeError):
        main_mod._build_source(settings, market=None)
