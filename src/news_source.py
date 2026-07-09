"""Polygon news source with a per-day disk cache.

Polygon's free tier serves historical news (title/description/tickers) back a
couple years, rate-limited to 5 req/min. We fetch the global feed one day at a
time (paginated), score each headline with the finance lexicon, and cache the
scored articles per day so repeated sentiment backtests never re-fetch. Old days
with no news are cached empty (stable historically).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from typing import Any

from src.polygon_http import get_json
from src.sentiment import score_text

_NEWS_URL = "https://api.polygon.io/v2/reference/news"


class PolygonNews:
    def __init__(self, api_key: str, cache_path: str = "", timeout: int = 20):
        if not api_key:
            raise RuntimeError("news sentiment requires POLYGON_API_KEY")
        self._api_key = api_key
        self._timeout = timeout
        self._disk: sqlite3.Connection | None = None
        if cache_path:
            if cache_path != ":memory:":
                Path(cache_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._disk = sqlite3.connect(cache_path)
            self._disk.execute(
                "CREATE TABLE IF NOT EXISTS news_cache "
                "(date TEXT PRIMARY KEY, articles_json TEXT NOT NULL)"
            )
            self._disk.commit()

    def fetch_day(self, date_iso: str) -> list[dict[str, Any]]:
        """Scored articles for `date_iso`: [{date, tickers, score}]. Cached."""
        if self._disk is not None:
            row = self._disk.execute(
                "SELECT articles_json FROM news_cache WHERE date=?", (date_iso,)
            ).fetchone()
            if row is not None:
                return json.loads(row[0])
        raw = self._fetch_raw_day(date_iso)
        arts = [
            {
                "date": date_iso,
                "tickers": r.get("tickers", []) or [],
                "score": score_text(f"{r.get('title', '')} {r.get('description', '')}"),
            }
            for r in raw
            if r.get("tickers")
        ]
        if self._disk is not None:
            self._disk.execute(
                "INSERT OR REPLACE INTO news_cache (date, articles_json) VALUES (?,?)",
                (date_iso, json.dumps(arts, separators=(",", ":"))),
            )
            self._disk.commit()
        return arts

    def _fetch_raw_day(self, date_iso: str) -> list[dict[str, Any]]:
        """All raw news articles published on `date_iso` (paginated)."""
        url = (
            f"{_NEWS_URL}?published_utc.gte={date_iso}&published_utc.lte={date_iso}"
            f"&order=asc&limit=1000&apiKey={self._api_key}"
        )
        out: list[dict[str, Any]] = []
        pages = 0
        while url and pages < 20:  # safety cap on pagination
            try:
                data = get_json(url, self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    break
                raise
            out.extend(data.get("results") or [])
            nxt = data.get("next_url")
            url = f"{nxt}&apiKey={self._api_key}" if nxt else ""
            pages += 1
        return out
