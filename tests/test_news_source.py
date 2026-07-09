"""Polygon news source with per-day disk cache."""

from __future__ import annotations

from src.news_source import PolygonNews


def test_fetch_day_scores_and_caches(tmp_path, monkeypatch):
    db = str(tmp_path / "news.db")
    raw = [
        {
            "tickers": ["AAA", "BBB"],
            "title": "AAA beats estimates, shares surge",
            "description": "record profit",
        },
        {"tickers": ["CCC"], "title": "CCC plunges on weak guidance", "description": "big loss"},
    ]
    calls = []

    def fake(self, date_iso):
        calls.append(date_iso)
        return raw

    monkeypatch.setattr(PolygonNews, "_fetch_raw_day", fake)
    news = PolygonNews("k", cache_path=db)
    arts = news.fetch_day("2026-06-01")
    assert len(arts) == 2
    a = {tuple(x["tickers"]): x for x in arts}
    assert a[("AAA", "BBB")]["score"] > 0  # positive headline
    assert a[("CCC",)]["score"] < 0  # negative headline
    assert all(x["date"] == "2026-06-01" for x in arts)

    # second instance on same cache file must not re-hit the API
    news2 = PolygonNews("k", cache_path=db)
    monkeypatch.setattr(PolygonNews, "_fetch_raw_day", fake)
    again = news2.fetch_day("2026-06-01")
    assert len(again) == 2
    assert calls == ["2026-06-01"]  # served from cache the 2nd time


def test_empty_day_cached(tmp_path, monkeypatch):
    db = str(tmp_path / "news.db")
    monkeypatch.setattr(PolygonNews, "_fetch_raw_day", lambda self, d: [])
    news = PolygonNews("k", cache_path=db)
    assert news.fetch_day("2026-06-02") == []
    # cached-empty: a fresh instance returns [] without a raw call
    hits = []
    monkeypatch.setattr(PolygonNews, "_fetch_raw_day", lambda self, d: hits.append(d) or [])
    assert PolygonNews("k", cache_path=db).fetch_day("2026-06-02") == []
    assert hits == []
