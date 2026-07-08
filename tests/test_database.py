"""Database: schema + audit-trail writes/reads on a tmp SQLite file."""

from __future__ import annotations

from datetime import datetime

from src.database import Database
from src.models import OrderSide
from src.short_accumulation import ShortSetupRecord


def _short_record(symbol="RUNR", strategy="fade", entry_mode="breakout", net=8.0):
    return ShortSetupRecord(
        symbol=symbol,
        session_date="2026-07-06",
        strategy=strategy,
        entry_mode=entry_mode,
        triggered=True,
        entry_price=16.05,
        net_return_pct=net,
        exit_reason="take_profit",
        held_min=12.0,
        max_adverse_pct=3.0,
        shortable=False,
        easy_to_borrow=False,
        run_gain_pct=None,
        is_fizzle=True,
        features={"vol_accel": 2.1, "gap_pct": 4.0},
    )


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


def test_record_short_setup_and_count(tmp_path):
    db = _db(tmp_path)
    rid = db.record_short_setup(_short_record())
    assert rid > 0
    assert db.count_short_setups() == 1
    rows = db.get_short_setups(session_date="2026-07-06")
    assert rows[0]["symbol"] == "RUNR"
    assert rows[0]["strategy"] == "fade"
    assert rows[0]["shortable"] == 0  # bool -> int
    assert rows[0]["net_return_pct"] == 8.0
    # features round-trip as json
    assert '"vol_accel"' in rows[0]["features_json"]


def test_record_short_setup_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.record_short_setup(_short_record(net=8.0))
    # same (session_date, symbol, strategy, entry_mode) upserts, not duplicates
    db.record_short_setup(_short_record(net=9.5))
    assert db.count_short_setups() == 1
    rows = db.get_short_setups(session_date="2026-07-06")
    assert rows[0]["net_return_pct"] == 9.5  # latest wins


def test_record_short_setup_distinct_strategy_and_mode(tmp_path):
    db = _db(tmp_path)
    db.record_short_setup(_short_record(strategy="fade", entry_mode="breakout"))
    db.record_short_setup(_short_record(strategy="exhaustion", entry_mode="breakdown"))
    assert db.count_short_setups() == 2


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
