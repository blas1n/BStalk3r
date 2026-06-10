"""screened table: durable longitudinal record of every screened runner."""

from __future__ import annotations

from datetime import UTC, datetime

from src.database import Database
from src.models import MarketSnapshot


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "s.db"))
    db.init_schema()
    return db


def _snap(symbol, ts, **over) -> MarketSnapshot:
    base = dict(
        symbol=symbol,
        last_price=8.5,
        day_change_pct=22.0,
        rvol=12.0,
        volume_acceleration=1.0,
        spread_pct=0.0,
        timestamp=ts,
    )
    base.update(over)
    return MarketSnapshot(**base)


def test_record_screened_persists_runners(tmp_path):
    db = _db(tmp_path)
    ts = datetime(2026, 6, 9, 16, 0, tzinfo=UTC)
    n = db.record_screened(
        [_snap("RUNR", ts), _snap("BLAZ", ts)], source="polygon", entry_ready={"BLAZ"}
    )
    assert n == 2
    assert db.count_screened() == 2
    rows = {r["symbol"]: r for r in db.get_screened("2026-06-09")}
    assert rows["RUNR"]["session_date"] == "2026-06-09"
    assert rows["RUNR"]["source"] == "polygon"
    assert rows["BLAZ"]["entry_ready"] == 1
    assert rows["RUNR"]["entry_ready"] == 0


def test_record_screened_is_idempotent_per_session(tmp_path):
    db = _db(tmp_path)
    ts = datetime(2026, 6, 9, 16, 0, tzinfo=UTC)
    db.record_screened([_snap("RUNR", ts, last_price=8.5)], source="polygon")
    # same session/symbol/source again with updated price -> upsert, no dup
    db.record_screened([_snap("RUNR", ts, last_price=9.1)], source="polygon")
    rows = db.get_screened("2026-06-09")
    assert len(rows) == 1
    assert rows[0]["last_price"] == 9.1


def test_same_symbol_different_source_kept_separate(tmp_path):
    db = _db(tmp_path)
    ts = datetime(2026, 6, 9, 16, 0, tzinfo=UTC)
    db.record_screened([_snap("RUNR", ts)], source="polygon")
    db.record_screened([_snap("RUNR", ts)], source="watchlist")
    assert db.count_screened("2026-06-09") == 2


def test_session_date_comes_from_snapshot_timestamp(tmp_path):
    db = _db(tmp_path)
    # captured on the 10th but data is the 9th session
    db.record_screened([_snap("RUNR", datetime(2026, 6, 9, 20, 0, tzinfo=UTC))], source="polygon")
    assert db.count_screened("2026-06-09") == 1
    assert db.count_screened("2026-06-10") == 0


def test_record_screened_without_timestamp_uses_today(tmp_path):
    db = _db(tmp_path)
    today = datetime.now(UTC).date().isoformat()
    db.record_screened([_snap("RUNR", None)], source="watchlist")
    assert db.count_screened(today) == 1
