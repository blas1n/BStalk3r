"""outcomes table: upsert + pending detection."""

from __future__ import annotations

from datetime import datetime

from src.database import Database
from src.models import MarketSnapshot


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "o.db"))
    db.init_schema()
    return db


def _screen(db, symbol, session_iso):
    ts = datetime.fromisoformat(session_iso + "T16:00:00+00:00")
    snap = MarketSnapshot(
        symbol=symbol,
        last_price=10.0,
        day_change_pct=20.0,
        rvol=12.0,
        volume_acceleration=1.0,
        spread_pct=0.0,
        timestamp=ts,
    )
    db.record_screened([snap], source="polygon")
    return db.get_screened(session_iso)[0]["id"]


def test_upsert_outcome_idempotent(tmp_path):
    db = _db(tmp_path)
    sid = _screen(db, "RUNR", "2026-05-26")
    db.upsert_outcome(sid, "RUNR", "2026-05-26", "1d", 10.0, 10.5, 5.0, 10.0, -5.0)
    db.upsert_outcome(sid, "RUNR", "2026-05-26", "1d", 10.0, 11.0, 10.0, 12.0, -3.0)
    rows = db.get_outcomes("RUNR")
    assert len(rows) == 1  # upsert, not dup
    assert rows[0]["fwd_return_pct"] == 10.0
    assert db.count_outcomes() == 1


def test_pending_excludes_too_recent_and_completed(tmp_path):
    db = _db(tmp_path)
    old = _screen(db, "OLD", "2026-05-26")  # old enough
    _screen(db, "NEW", "2026-06-10")  # too recent
    done = _screen(db, "DONE", "2026-05-20")  # old + already has 5d
    db.upsert_outcome(done, "DONE", "2026-05-20", "5d", 10.0, 11.0, 10.0, 15.0, -4.0)

    pending = db.get_screened_pending_outcomes(before_date="2026-06-01")
    ids = {r["id"] for r in pending}
    assert old in ids
    assert done not in ids  # has final horizon
    assert all(r["symbol"] != "NEW" for r in pending)  # cutoff excludes recent


def test_pending_partial_horizon_still_pending(tmp_path):
    db = _db(tmp_path)
    sid = _screen(db, "PART", "2026-05-26")
    # only 1d filled -> still pending on the 5d final horizon
    db.upsert_outcome(sid, "PART", "2026-05-26", "1d", 10.0, 10.5, 5.0, 10.0, -5.0)
    pending = db.get_screened_pending_outcomes(before_date="2026-06-01")
    assert sid in {r["id"] for r in pending}
