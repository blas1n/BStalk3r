"""Replay DB loader + `replay` command over seeded screened+outcome data."""

from __future__ import annotations

from datetime import datetime

import src.main as main_mod
from src.config import Settings
from src.database import Database
from src.models import MarketSnapshot


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "r.db"))
    db.init_schema()
    return db


def _seed(db, symbol, session, price, chg, rvol, *, ret=None, source="polygon"):
    ts = datetime.fromisoformat(session + "T16:00:00+00:00")
    db.record_screened(
        [MarketSnapshot(symbol, price, chg, rvol, 1.0, 0.0, timestamp=ts)], source=source
    )
    sid = next(r["id"] for r in db.get_screened(session) if r["symbol"] == symbol)
    if ret is not None:
        db.upsert_outcome(sid, symbol, session, "3d", price, price * (1 + ret / 100), ret, ret, 0.0)
    return sid


def test_loader_joins_outcome_at_horizon(tmp_path):
    db = _db(tmp_path)
    _seed(db, "WIN", "2026-06-01", 8.0, 20.0, 12.0, ret=10.0)
    _seed(db, "PEND", "2026-06-01", 6.0, 15.0, 9.0, ret=None)

    rows = {r["symbol"]: r for r in db.get_screened_with_outcomes("3d")}
    assert rows["WIN"]["ret"] == 10.0
    assert rows["PEND"]["ret"] is None  # left join, no outcome yet


def test_loader_filters_by_date_and_source(tmp_path):
    db = _db(tmp_path)
    _seed(db, "OLD", "2026-05-20", 8.0, 20.0, 12.0, ret=5.0)
    _seed(db, "NEW", "2026-06-10", 8.0, 20.0, 12.0, ret=5.0)
    _seed(db, "WL", "2026-06-10", 8.0, 20.0, 12.0, ret=5.0, source="watchlist")

    in_range = {r["symbol"] for r in db.get_screened_with_outcomes("3d", start_date="2026-06-01")}
    assert in_range == {"NEW", "WL"}
    poly = {r["symbol"] for r in db.get_screened_with_outcomes("3d", source="polygon")}
    assert "WL" not in poly


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "r.db"),
        min_rvol=8.0,
        min_volume_acceleration=3.0,
    )


def test_replay_cmd_runs_baseline_and_sweep(tmp_path, capsys):
    db = _db(tmp_path)
    _seed(db, "WIN1", "2026-06-01", 8.0, 20.0, 12.0, ret=10.0)
    _seed(db, "WIN2", "2026-06-01", 4.0, 30.0, 20.0, ret=6.0)
    _seed(db, "LOSS", "2026-06-01", 6.0, 10.0, 9.0, ret=-4.0)

    rc = main_mod.cmd_replay(_settings(tmp_path), db, horizon="3d", sweep_rvol=[8.0, 15.0])
    assert rc == 0
    out = capsys.readouterr().out
    assert "baseline" in out
    assert "rvol>=15" in out
    assert "Replay over 3 screened runner(s)" in out
