"""SQLite audit trail: every signal, order, position and daily roll-up.

Plain stdlib sqlite3 (no async, no ORM) — the realtime loop writes a handful
of rows per minute, so simplicity and durability win. Timestamps are stored as
ISO-8601 UTC-ish strings; date filtering uses substr on the first 10 chars.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models import OrderSide

_SCHEMA = """
CREATE TABLE IF NOT EXISTS param_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    universe_source TEXT,
    dry_run INTEGER NOT NULL DEFAULT 1,
    param_set_id INTEGER,
    git_commit TEXT,
    notes TEXT
);

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
    reason_json TEXT,
    run_id INTEGER
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
    reason TEXT,
    run_id INTEGER
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
    exit_reason TEXT,
    run_id INTEGER
);

CREATE TABLE IF NOT EXISTS screened (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screened_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    last_price REAL,
    day_change_pct REAL,
    rvol REAL,
    volume_acceleration REAL,
    spread_pct REAL,
    entry_ready INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER,
    UNIQUE(session_date, symbol, source)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screened_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    base_date TEXT NOT NULL,
    horizon TEXT NOT NULL,
    ref_price REAL,
    fwd_price REAL,
    fwd_return_pct REAL,
    max_gain_pct REAL,
    max_drawdown_pct REAL,
    computed_at TEXT NOT NULL,
    UNIQUE(screened_id, horizon)
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

    # ---- provenance (runs + parameter sets) ----
    def start_run(
        self,
        params: dict[str, Any],
        mode: str,
        universe_source: str,
        dry_run: bool,
        git_commit: str | None = None,
        notes: str = "",
    ) -> int:
        """Open a run tied to the exact parameter set; return its run_id.

        Identical params dedupe to one param_set (hashed canonical json) so the
        same config across many runs is comparable.
        """
        param_set_id = self._ensure_param_set(params)
        cur = self.conn.execute(
            """INSERT INTO runs
               (started_at, mode, universe_source, dry_run, param_set_id, git_commit, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (
                datetime.now(UTC).isoformat(),
                mode,
                universe_source,
                1 if dry_run else 0,
                param_set_id,
                git_commit,
                notes,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _ensure_param_set(self, params: dict[str, Any]) -> int:
        canonical = json.dumps(params, sort_keys=True, default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        self.conn.execute(
            "INSERT OR IGNORE INTO param_sets (hash, params_json, created_at) VALUES (?,?,?)",
            (digest, canonical, datetime.now(UTC).isoformat()),
        )
        row = self.conn.execute("SELECT id FROM param_sets WHERE hash=?", (digest,)).fetchone()
        return int(row["id"])

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_param_set(self, param_set_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM param_sets WHERE id=?", (param_set_id,)).fetchone()
        return dict(row) if row else None

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
        run_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals
               (timestamp, symbol, price, day_change_pct, rvol,
                volume_acceleration, spread_pct, score, reason_json, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
                run_id,
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
        run_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders
               (timestamp, alpaca_order_id, symbol, side, qty,
                order_type, limit_price, status, reason, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
                run_id,
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
        self,
        symbol: str,
        entry_time: datetime,
        entry_price: float,
        qty: int,
        run_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO positions (symbol, entry_time, entry_price, qty, current_status, run_id)
               VALUES (?,?,?,?, 'open', ?)""",
            (symbol, _iso(entry_time), entry_price, qty, run_id),
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

    # ---- outcomes (forward returns on screened runners) ----
    def upsert_outcome(
        self,
        screened_id: int,
        symbol: str,
        base_date: str,
        horizon: str,
        ref_price: float,
        fwd_price: float,
        fwd_return_pct: float,
        max_gain_pct: float,
        max_drawdown_pct: float,
    ) -> None:
        self.conn.execute(
            """INSERT INTO outcomes
               (screened_id, symbol, base_date, horizon, ref_price, fwd_price,
                fwd_return_pct, max_gain_pct, max_drawdown_pct, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(screened_id, horizon) DO UPDATE SET
                 ref_price=excluded.ref_price,
                 fwd_price=excluded.fwd_price,
                 fwd_return_pct=excluded.fwd_return_pct,
                 max_gain_pct=excluded.max_gain_pct,
                 max_drawdown_pct=excluded.max_drawdown_pct,
                 computed_at=excluded.computed_at""",
            (
                screened_id,
                symbol,
                base_date,
                horizon,
                ref_price,
                fwd_price,
                fwd_return_pct,
                max_gain_pct,
                max_drawdown_pct,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def count_outcomes(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM outcomes").fetchone()["c"])

    def get_outcomes(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM outcomes WHERE symbol=? ORDER BY base_date, horizon", (symbol,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM outcomes ORDER BY base_date, symbol, horizon"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_screened_pending_outcomes(
        self, before_date: str, final_horizon: str = "5d"
    ) -> list[dict[str, Any]]:
        """Screened rows old enough to track that still lack the final horizon.

        `before_date` is the newest session to consider (caller passes today minus
        the free-tier lag). Re-running fills shorter horizons as data appears.
        """
        rows = self.conn.execute(
            """SELECT s.* FROM screened s
               WHERE s.session_date <= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM outcomes o
                   WHERE o.screened_id = s.id AND o.horizon = ?
                 )
               ORDER BY s.session_date, s.symbol""",
            (before_date, final_horizon),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_screened_band(
        self,
        min_price: float,
        max_price: float,
        limit: int | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Screened runners inside the tradeable price band, newest first.

        The intraday backtest re-derives prior close from `day_change_pct`, so
        rows with a non-positive change (can't back out prev close) are excluded.
        """
        sql = "SELECT * FROM screened WHERE last_price BETWEEN ? AND ? AND day_change_pct > -100"
        params: list[Any] = [min_price, max_price]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY session_date DESC, day_change_pct DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_screened_with_outcomes(
        self,
        horizon: str,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Screened runners joined to their forward outcome at `horizon`.

        `ret` / `max_gain` / `max_drawdown` are NULL when the outcome isn't
        computed yet. This is the replay harness's input.
        """
        sql = """
            SELECT s.*,
                   o.fwd_return_pct AS ret,
                   o.max_gain_pct AS max_gain,
                   o.max_drawdown_pct AS max_drawdown
            FROM screened s
            LEFT JOIN outcomes o ON o.screened_id = s.id AND o.horizon = ?
            WHERE 1=1
        """
        params: list[Any] = [horizon]
        if start_date:
            sql += " AND s.session_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND s.session_date <= ?"
            params.append(end_date)
        if source:
            sql += " AND s.source = ?"
            params.append(source)
        sql += " ORDER BY s.session_date, s.symbol"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- screened (longitudinal runner dataset) ----
    def record_screened(
        self,
        snapshots: list[Any],
        source: str,
        entry_ready: set[str] | None = None,
        run_id: int | None = None,
    ) -> int:
        """Persist every screened runner, idempotent per (session_date, symbol, source).

        session_date comes from each snapshot's timestamp (the trading date the
        data represents) so a Monday capture of Friday's runners files correctly.
        """
        ready = entry_ready or set()
        count = 0
        for s in snapshots:
            ts = s.timestamp or datetime.now(UTC)
            self.conn.execute(
                """INSERT INTO screened
                   (screened_at, session_date, symbol, source, last_price,
                    day_change_pct, rvol, volume_acceleration, spread_pct, entry_ready, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_date, symbol, source) DO UPDATE SET
                     screened_at=excluded.screened_at,
                     last_price=excluded.last_price,
                     day_change_pct=excluded.day_change_pct,
                     rvol=excluded.rvol,
                     volume_acceleration=excluded.volume_acceleration,
                     spread_pct=excluded.spread_pct,
                     entry_ready=excluded.entry_ready,
                     run_id=excluded.run_id""",
                (
                    datetime.now(UTC).isoformat(),
                    ts.date().isoformat(),
                    s.symbol,
                    source,
                    s.last_price,
                    s.day_change_pct,
                    s.rvol,
                    s.volume_acceleration,
                    s.spread_pct,
                    1 if s.symbol in ready else 0,
                    run_id,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def count_screened(self, date: str | None = None) -> int:
        if date:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM screened WHERE session_date=?", (date,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) c FROM screened").fetchone()
        return int(row["c"])

    def get_screened(self, date: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM screened WHERE session_date=? ORDER BY day_change_pct DESC", (date,)
        ).fetchall()
        return [dict(r) for r in rows]
