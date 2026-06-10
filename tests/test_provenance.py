"""Provenance: every decision attributable to a parameter set + run."""

from __future__ import annotations

from datetime import UTC, datetime

from src.database import Database
from src.models import MarketSnapshot, OrderSide

PARAMS = {"stop_loss_pct": 0.05, "min_rvol": 8.0}


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "p.db"))
    db.init_schema()
    return db


def test_start_run_creates_run_and_param_set(tmp_path):
    db = _db(tmp_path)
    rid = db.start_run(
        PARAMS, mode="scan", universe_source="polygon", dry_run=True, git_commit="abc123"
    )
    assert rid > 0
    run = db.get_run(rid)
    assert run["mode"] == "scan"
    assert run["git_commit"] == "abc123"
    assert run["dry_run"] == 1
    ps = db.get_param_set(run["param_set_id"])
    assert ps["params_json"]  # canonical json stored
    assert "stop_loss_pct" in ps["params_json"]


def test_identical_params_dedupe_to_one_param_set(tmp_path):
    db = _db(tmp_path)
    r1 = db.start_run(PARAMS, mode="run", universe_source="watchlist", dry_run=True)
    r2 = db.start_run(dict(PARAMS), mode="run", universe_source="watchlist", dry_run=True)
    assert r1 != r2  # distinct runs
    assert db.get_run(r1)["param_set_id"] == db.get_run(r2)["param_set_id"]


def test_changed_params_make_new_param_set(tmp_path):
    db = _db(tmp_path)
    r1 = db.start_run(PARAMS, mode="run", universe_source="watchlist", dry_run=True)
    r2 = db.start_run(
        {**PARAMS, "stop_loss_pct": 0.07}, mode="run", universe_source="watchlist", dry_run=True
    )
    assert db.get_run(r1)["param_set_id"] != db.get_run(r2)["param_set_id"]


def test_run_id_stamped_on_signal_order_position(tmp_path):
    db = _db(tmp_path)
    rid = db.start_run(PARAMS, mode="run", universe_source="polygon", dry_run=True)

    sid = db.insert_signal(
        symbol="RUNR",
        price=8.5,
        day_change_pct=18.0,
        rvol=12.0,
        volume_acceleration=5.0,
        spread_pct=0.4,
        score=42.0,
        reason={},
        run_id=rid,
    )
    oid = db.insert_order(
        symbol="RUNR",
        side=OrderSide.BUY,
        qty=100,
        order_type="limit",
        limit_price=8.52,
        status="dry_run",
        reason="x",
        run_id=rid,
    )
    pid = db.insert_position(
        symbol="RUNR",
        entry_time=datetime(2026, 6, 10, tzinfo=UTC),
        entry_price=8.5,
        qty=100,
        run_id=rid,
    )
    assert db.get_order(oid)["run_id"] == rid
    row = db.conn.execute("SELECT run_id FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["run_id"] == rid
    prow = db.conn.execute("SELECT run_id FROM positions WHERE id=?", (pid,)).fetchone()
    assert prow["run_id"] == rid


def test_run_id_stamped_on_screened(tmp_path):
    db = _db(tmp_path)
    rid = db.start_run(PARAMS, mode="scan", universe_source="polygon", dry_run=True)
    snap = MarketSnapshot(
        symbol="RUNR",
        last_price=8.5,
        day_change_pct=22.0,
        rvol=12.0,
        volume_acceleration=1.0,
        spread_pct=0.0,
        timestamp=datetime(2026, 6, 9, 16, 0, tzinfo=UTC),
    )
    db.record_screened([snap], source="polygon", run_id=rid)
    row = db.get_screened("2026-06-09")[0]
    assert row["run_id"] == rid
