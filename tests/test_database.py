"""Database: schema + audit-trail writes/reads on a tmp SQLite file."""

from __future__ import annotations

from datetime import datetime

from src.database import Database
from src.models import OrderSide


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_init_schema_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.init_schema()  # second call must not raise
    assert db.count_signals() == 0


def test_insert_signal_and_count(tmp_path):
    db = _db(tmp_path)
    sid = db.insert_signal(
        symbol="RUNR",
        price=8.5,
        day_change_pct=18.0,
        rvol=12.0,
        volume_acceleration=5.0,
        spread_pct=0.4,
        score=42.0,
        reason={"rvol": True},
    )
    assert sid > 0
    assert db.count_signals() == 1


def test_order_insert_then_status_update(tmp_path):
    db = _db(tmp_path)
    oid = db.insert_order(
        symbol="RUNR",
        side=OrderSide.BUY,
        qty=100,
        order_type="limit",
        limit_price=8.52,
        status="dry_run",
        reason="entry signal",
    )
    db.update_order_status(oid, status="filled", alpaca_order_id="abc-123")
    row = db.get_order(oid)
    assert row["status"] == "filled"
    assert row["alpaca_order_id"] == "abc-123"
    assert row["side"] == "buy"


def test_position_lifecycle_and_pnl(tmp_path):
    db = _db(tmp_path)
    pid = db.insert_position(
        symbol="RUNR", entry_time=datetime(2026, 6, 10, 14, 0), entry_price=10.0, qty=100
    )
    db.close_position(
        pid,
        exit_time=datetime(2026, 6, 10, 14, 20),
        exit_price=11.5,
        pnl_pct=0.15,
        pnl_amount=150.0,
        exit_reason="take_profit_scale",
    )
    closed = db.get_closed_positions()
    assert len(closed) == 1
    assert closed[0]["pnl_amount"] == 150.0
    assert closed[0]["current_status"] == "closed"


def test_open_positions_excludes_closed(tmp_path):
    db = _db(tmp_path)
    db.insert_position(symbol="A", entry_time=datetime(2026, 6, 10), entry_price=5.0, qty=10)
    pid = db.insert_position(symbol="B", entry_time=datetime(2026, 6, 10), entry_price=6.0, qty=10)
    db.close_position(
        pid,
        exit_time=datetime(2026, 6, 10),
        exit_price=6.6,
        pnl_pct=0.1,
        pnl_amount=6.0,
        exit_reason="max_hold",
    )
    open_syms = {p["symbol"] for p in db.get_open_positions()}
    assert open_syms == {"A"}
