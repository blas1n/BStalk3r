"""SQLite audit trail: every signal, order, position and daily roll-up.

Plain stdlib sqlite3 (no async, no ORM) — the realtime loop writes a handful
of rows per minute, so simplicity and durability win. Timestamps are stored as
ISO-8601 UTC-ish strings; date filtering uses substr on the first 10 chars.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models import OrderSide

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    day_change_pct REAL,
    rvol REAL,
    volume_acceleration REAL,
    spread_pct REAL,
    score REAL,
    reason_json TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    alpaca_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty INTEGER NOT NULL,
    current_status TEXT NOT NULL DEFAULT 'open',
    exit_time TEXT,
    exit_price REAL,
    pnl_pct REAL,
    pnl_amount REAL,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    num_signals INTEGER,
    num_trades INTEGER,
    win_rate REAL,
    total_pnl REAL,
    max_drawdown REAL,
    notes TEXT
);
"""


def _iso(ts: datetime | None) -> str:
    return (ts or datetime.now(UTC)).isoformat()


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    # ---- schema ----
    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- signals ----
    def insert_signal(
        self,
        symbol: str,
        price: float,
        day_change_pct: float,
        rvol: float,
        volume_acceleration: float,
        spread_pct: float,
        score: float,
        reason: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals
               (timestamp, symbol, price, day_change_pct, rvol,
                volume_acceleration, spread_pct, score, reason_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _iso(timestamp),
                symbol,
                price,
                day_change_pct,
                rvol,
                volume_acceleration,
                spread_pct,
                score,
                json.dumps(reason or {}),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def count_signals(self, date: str | None = None) -> int:
        if date:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM signals WHERE substr(timestamp,1,10)=?", (date,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) c FROM signals").fetchone()
        return int(row["c"])

    # ---- orders ----
    def insert_order(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: int,
        order_type: str,
        limit_price: float | None,
        status: str,
        reason: str | None = None,
        alpaca_order_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders
               (timestamp, alpaca_order_id, symbol, side, qty,
                order_type, limit_price, status, reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _iso(timestamp),
                alpaca_order_id,
                symbol,
                str(getattr(side, "value", side)),
                qty,
                order_type,
                limit_price,
                status,
                reason,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_order_status(
        self, order_id: int, status: str, alpaca_order_id: str | None = None
    ) -> None:
        if alpaca_order_id is not None:
            self.conn.execute(
                "UPDATE orders SET status=?, alpaca_order_id=? WHERE id=?",
                (status, alpaca_order_id, order_id),
            )
        else:
            self.conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        self.conn.commit()

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def count_trades(self, date: str | None = None) -> int:
        """Filled/accepted BUY orders count as trades for the daily cap."""
        sql = "SELECT COUNT(*) c FROM orders WHERE side='buy' AND status!='error'"
        params: tuple = ()
        if date:
            sql += " AND substr(timestamp,1,10)=?"
            params = (date,)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    # ---- positions ----
    def insert_position(
        self, symbol: str, entry_time: datetime, entry_price: float, qty: int
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO positions (symbol, entry_time, entry_price, qty, current_status)
               VALUES (?,?,?,?, 'open')""",
            (symbol, _iso(entry_time), entry_price, qty),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_position(
        self,
        position_id: int,
        exit_time: datetime,
        exit_price: float,
        pnl_pct: float,
        pnl_amount: float,
        exit_reason: str,
    ) -> None:
        self.conn.execute(
            """UPDATE positions
               SET current_status='closed', exit_time=?, exit_price=?,
                   pnl_pct=?, pnl_amount=?, exit_reason=?
               WHERE id=?""",
            (_iso(exit_time), exit_price, pnl_pct, pnl_amount, exit_reason, position_id),
        )
        self.conn.commit()

    def get_open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE current_status='open' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions(self, date: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM positions WHERE current_status='closed'"
        params: tuple = ()
        if date:
            sql += " AND substr(exit_time,1,10)=?"
            params = (date,)
        sql += " ORDER BY exit_time, id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- daily stats ----
    def upsert_daily_stats(
        self,
        date: str,
        num_signals: int,
        num_trades: int,
        win_rate: float,
        total_pnl: float,
        max_drawdown: float,
        notes: str = "",
    ) -> None:
        self.conn.execute(
            """INSERT INTO daily_stats
               (date, num_signals, num_trades, win_rate, total_pnl, max_drawdown, notes)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 num_signals=excluded.num_signals,
                 num_trades=excluded.num_trades,
                 win_rate=excluded.win_rate,
                 total_pnl=excluded.total_pnl,
                 max_drawdown=excluded.max_drawdown,
                 notes=excluded.notes""",
            (date, num_signals, num_trades, win_rate, total_pnl, max_drawdown, notes),
        )
        self.conn.commit()

    def get_daily_stats(self, date: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM daily_stats WHERE date=?", (date,)).fetchone()
        return dict(row) if row else None
