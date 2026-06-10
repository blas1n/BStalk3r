"""Reporter: end-of-day aggregation from the SQLite audit trail."""

from __future__ import annotations

from datetime import datetime

from src.database import Database
from src.reporter import Reporter


def _seed(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    db.init_schema()
    day = datetime(2026, 6, 10, 14, 0)
    for i in range(3):
        db.insert_signal(
            symbol=f"S{i}",
            price=10.0,
            day_change_pct=10.0,
            rvol=10.0,
            volume_acceleration=4.0,
            spread_pct=0.3,
            score=10.0,
            reason={},
            timestamp=day,
        )
    # two winners, one loser
    for pnl in (150.0, 80.0, -60.0):
        pid = db.insert_position(symbol="X", entry_time=day, entry_price=10.0, qty=10)
        db.close_position(
            pid,
            exit_time=day,
            exit_price=10.0 + pnl / 10,
            pnl_pct=pnl / 100,
            pnl_amount=pnl,
            exit_reason="x",
        )
    return db


def test_report_aggregates_counts_and_pnl(tmp_path):
    db = _seed(tmp_path)
    rep = Reporter(db, str(tmp_path / "reports"))
    r = rep.build_report("2026-06-10")
    assert r["num_signals"] == 3
    assert r["num_trades"] == 3
    assert abs(r["total_pnl"] - 170.0) < 1e-6
    assert abs(r["win_rate"] - (2 / 3)) < 1e-6


def test_max_drawdown_from_equity_curve(tmp_path):
    db = _seed(tmp_path)
    rep = Reporter(db, str(tmp_path / "reports"))
    r = rep.build_report("2026-06-10")
    # curve: +150 -> +230 -> +170, peak 230, trough after 170 -> drawdown 60
    assert r["max_drawdown"] >= 60.0 - 1e-6


def test_write_report_creates_files_and_daily_stats(tmp_path):
    db = _seed(tmp_path)
    rep = Reporter(db, str(tmp_path / "reports"))
    paths = rep.write_report("2026-06-10")
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    stats = db.get_daily_stats("2026-06-10")
    assert stats is not None
    assert stats["num_trades"] == 3
