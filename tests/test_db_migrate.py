"""Schema migration: existing DBs predating run_id get the column added."""

from __future__ import annotations

import sqlite3

from src.database import Database
from src.models import OrderSide


def test_init_schema_adds_missing_run_id_columns(tmp_path):
    path = str(tmp_path / "old.db")
    # simulate an OLD orders table with no run_id (like the production DB)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "alpaca_order_id TEXT, symbol TEXT, side TEXT, qty INTEGER, order_type TEXT, "
        "limit_price REAL, status TEXT, reason TEXT)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.init_schema()  # must ALTER-add run_id, not silently skip
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(orders)")}
    assert "run_id" in cols
    # and an insert with run_id now works
    oid = db.insert_order(
        symbol="X",
        side=OrderSide.BUY,
        qty=1,
        order_type="limit",
        limit_price=1.0,
        status="dry_run",
        reason="t",
        run_id=7,
    )
    assert db.get_order(oid)["run_id"] == 7


def test_init_schema_idempotent_on_fresh_db(tmp_path):
    db = Database(str(tmp_path / "fresh.db"))
    db.init_schema()
    db.init_schema()  # second call (columns already present) must not raise
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(orders)")}
    assert "run_id" in cols
