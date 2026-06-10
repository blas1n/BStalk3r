"""`track` command: forward bars (faked) -> outcomes, idempotent."""

from __future__ import annotations

from datetime import datetime

import src.main as main_mod
from src.config import Settings
from src.database import Database
from src.models import MarketSnapshot

FWD = [
    {"high": 11.0, "low": 9.5, "close": 10.5},
    {"high": 12.0, "low": 10.0, "close": 11.5},
    {"high": 11.8, "low": 10.5, "close": 11.0},
    {"high": 13.0, "low": 10.8, "close": 12.5},
    {"high": 12.0, "low": 9.0, "close": 9.5},
]


class _FakeBars:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return self.bars


def _settings(tmp_path):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        db_path=str(tmp_path / "t.db"),
    )


def _seed_old_runner(db, symbol="RUNR", session="2026-05-26"):
    ts = datetime.fromisoformat(session + "T16:00:00+00:00")
    db.record_screened(
        [MarketSnapshot(symbol, 10.0, 20.0, 12.0, 1.0, 0.0, timestamp=ts)], source="polygon"
    )


def test_track_writes_outcomes_for_old_runner(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed_old_runner(db)

    rc = main_mod.cmd_track(settings, db, _FakeBars(FWD), before_date="2026-06-01", throttle_sec=0)
    assert rc == 0
    outs = {o["horizon"]: o for o in db.get_outcomes("RUNR")}
    assert set(outs) == {"1d", "3d", "5d"}
    assert abs(outs["5d"]["fwd_return_pct"] - (-5.0)) < 1e-6
    assert abs(outs["5d"]["max_gain_pct"] - 30.0) < 1e-6


def test_track_is_idempotent_and_clears_pending(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed_old_runner(db)
    bars = _FakeBars(FWD)

    main_mod.cmd_track(settings, db, bars, before_date="2026-06-01", throttle_sec=0)
    main_mod.cmd_track(settings, db, bars, before_date="2026-06-01", throttle_sec=0)
    assert db.count_outcomes() == 3  # upsert, no dupes
    # nothing left pending once the final horizon is filled
    assert db.get_screened_pending_outcomes(before_date="2026-06-01") == []


def test_track_window_targets_sessions_after_base(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed_old_runner(db, session="2026-05-26")
    bars = _FakeBars(FWD)

    main_mod.cmd_track(settings, db, bars, before_date="2026-06-01", throttle_sec=0)
    symbol, start, end = bars.calls[0]
    assert start == "2026-05-27"  # day after base
    assert end > start


def test_track_skips_runner_without_forward_data_yet(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    db.init_schema()
    _seed_old_runner(db)

    # bars provider returns nothing (free-tier 403 -> []) -> stays pending
    main_mod.cmd_track(settings, db, _FakeBars([]), before_date="2026-06-01", throttle_sec=0)
    assert db.count_outcomes() == 0
    assert len(db.get_screened_pending_outcomes(before_date="2026-06-01")) == 1
