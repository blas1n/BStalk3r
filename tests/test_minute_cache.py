"""Persistent minute-bar cache — a drop-in MinuteBarsProvider wrapper.

Polygon free tier is capped at 5 req/min, so iterating many strategies × params
over a window re-pays that latency on every run. Caching fetched bars to SQLite
means each (symbol, date) hits the API at most once ever; subsequent backtests
read from disk and run instantly over long windows. Empty results are cached too
(a historical date with no data won't gain any), so known-empty days aren't
re-fetched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.minute_cache import CachedMinuteBars


class _CountingProvider:
    """Fake inner provider: records fetches, returns preset bars per (sym, date)."""

    def __init__(self, by_key):
        self._by = by_key
        self.calls: list[tuple[str, str]] = []

    def fetch(self, symbol, date):
        self.calls.append((symbol, date))
        return self._by.get((symbol, date), [])


def _bar(minute, close):
    return {
        "ts": datetime(2026, 7, 6, 13, 30 + minute, tzinfo=UTC),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
    }


def test_miss_delegates_then_hit_serves_from_cache(tmp_path):
    inner = _CountingProvider({("RUNR", "2026-07-06"): [_bar(0, 10.0), _bar(1, 10.5)]})
    cache = CachedMinuteBars(inner, str(tmp_path / "cache.db"))
    first = cache.fetch("RUNR", "2026-07-06")
    second = cache.fetch("RUNR", "2026-07-06")
    assert len(first) == 2 and len(second) == 2
    assert inner.calls == [("RUNR", "2026-07-06")]  # API hit exactly once


def test_bars_round_trip_preserves_types(tmp_path):
    inner = _CountingProvider({("RUNR", "2026-07-06"): [_bar(3, 12.34)]})
    cache = CachedMinuteBars(inner, str(tmp_path / "cache.db"))
    cache.fetch("RUNR", "2026-07-06")  # populate
    got = cache.fetch("RUNR", "2026-07-06")[0]  # from cache
    assert isinstance(got["ts"], datetime)
    assert got["ts"] == datetime(2026, 7, 6, 13, 33, tzinfo=UTC)
    assert got["close"] == 12.34 and got["volume"] == 1000.0


def test_empty_result_is_cached_not_refetched(tmp_path):
    inner = _CountingProvider({})  # no data for anything
    cache = CachedMinuteBars(inner, str(tmp_path / "cache.db"))
    assert cache.fetch("NODATA", "2026-07-06") == []
    assert cache.fetch("NODATA", "2026-07-06") == []
    assert inner.calls == [("NODATA", "2026-07-06")]  # empty cached, one hit


def test_distinct_keys_are_separate(tmp_path):
    inner = _CountingProvider(
        {
            ("AAA", "2026-07-06"): [_bar(0, 1.0)],
            ("AAA", "2026-07-07"): [_bar(0, 2.0)],
            ("BBB", "2026-07-06"): [_bar(0, 3.0)],
        }
    )
    cache = CachedMinuteBars(inner, str(tmp_path / "cache.db"))
    assert cache.fetch("AAA", "2026-07-06")[0]["close"] == 1.0
    assert cache.fetch("AAA", "2026-07-07")[0]["close"] == 2.0
    assert cache.fetch("BBB", "2026-07-06")[0]["close"] == 3.0
    assert len(inner.calls) == 3


def test_persists_across_instances(tmp_path):
    db = str(tmp_path / "cache.db")
    inner1 = _CountingProvider({("RUNR", "2026-07-06"): [_bar(0, 9.0)]})
    CachedMinuteBars(inner1, db).fetch("RUNR", "2026-07-06")
    # a fresh cache object on the same file should not re-hit the API
    inner2 = _CountingProvider({("RUNR", "2026-07-06"): [_bar(0, 9.0)]})
    got = CachedMinuteBars(inner2, db).fetch("RUNR", "2026-07-06")
    assert got[0]["close"] == 9.0
    assert inner2.calls == []  # served from persisted cache

    def stats(cache):
        return cache.stats()

    assert stats(CachedMinuteBars(inner2, db))["symbol_days"] == 1
