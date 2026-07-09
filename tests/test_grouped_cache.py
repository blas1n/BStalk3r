"""Persistent grouped-daily cache on PolygonGroupedSource.

Grouped bars are the other repeated backtest fetch (one call per session, ~12k
symbols). Minute bars are already cached (#22); this closes the gap so a warm
re-run doesn't re-pay grouped 429 backoffs. Only *non-empty* results are cached —
a too-recent date 403s to [] now but gains data later, so caching empties would
serve stale [] and break session resolution.
"""

from __future__ import annotations

from src.sources import PolygonGroupedSource, ScreenBounds

BOUNDS = ScreenBounds(min_price=1.0, max_price=50.0, min_change_pct=5.0)


class _Counter:
    def __init__(self, by_date):
        self.by = by_date
        self.calls: list[str] = []

    def __call__(self, date_iso):
        self.calls.append(date_iso)
        return {"results": self.by.get(date_iso, [])}


def test_grouped_disk_cache_hits_raw_once(tmp_path, monkeypatch):
    db = str(tmp_path / "g.db")
    raw = _Counter({"2026-07-02": [{"T": "AAA", "c": 5.0}]})
    src = PolygonGroupedSource("k", BOUNDS, cache_path=db)
    monkeypatch.setattr(src, "_fetch_raw", raw)
    first = src.fetch_grouped("2026-07-02")
    # a NEW instance on the same file must not re-hit raw
    src2 = PolygonGroupedSource("k", BOUNDS, cache_path=db)
    raw2 = _Counter({"2026-07-02": [{"T": "AAA", "c": 5.0}]})
    monkeypatch.setattr(src2, "_fetch_raw", raw2)
    second = src2.fetch_grouped("2026-07-02")
    assert first == second == [{"T": "AAA", "c": 5.0}]
    assert raw.calls == ["2026-07-02"]
    assert raw2.calls == []  # served from persisted cache


def test_empty_result_is_not_cached(tmp_path, monkeypatch):
    db = str(tmp_path / "g.db")
    raw = _Counter({})  # no data -> []
    src = PolygonGroupedSource("k", BOUNDS, cache_path=db)
    monkeypatch.setattr(src, "_fetch_raw", raw)
    assert src.fetch_grouped("2026-07-04") == []
    # a fresh instance re-checks (empty not persisted) so a later-landing date works
    src2 = PolygonGroupedSource("k", BOUNDS, cache_path=db)
    raw2 = _Counter({"2026-07-04": [{"T": "LATE", "c": 3.0}]})
    monkeypatch.setattr(src2, "_fetch_raw", raw2)
    assert src2.fetch_grouped("2026-07-04") == [{"T": "LATE", "c": 3.0}]
    assert raw2.calls == ["2026-07-04"]


def test_no_cache_path_still_works(tmp_path, monkeypatch):
    raw = _Counter({"2026-07-02": [{"T": "AAA", "c": 5.0}]})
    src = PolygonGroupedSource("k", BOUNDS)  # cache disabled
    monkeypatch.setattr(src, "_fetch_raw", raw)
    assert src.fetch_grouped("2026-07-02") == [{"T": "AAA", "c": 5.0}]
    # in-memory cache still dedupes within the instance
    src.fetch_grouped("2026-07-02")
    assert raw.calls == ["2026-07-02"]
