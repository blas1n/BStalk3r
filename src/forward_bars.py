"""Forward daily bars provider (Polygon aggregates, free tier).

Fetches the daily bars *after* a runner's session so outcomes can be computed.
Free tier serves historical EOD aggregates but 403s on too-recent dates, so the
tracker only processes runners old enough that the forward window is available;
a 403/404 here is treated as "not available yet" (empty) and left pending.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

POLYGON_AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"


class ForwardBarsProvider(Protocol):
    def fetch(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]: ...


def polygon_aggs_to_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Polygon agg rows (t/o/h/l/c/v) to {date,high,low,close}, ascending."""
    rows = payload.get("results") or []
    bars: list[dict[str, Any]] = []
    for r in rows:
        if r.get("h") is None or r.get("l") is None or r.get("c") is None:
            continue
        bars.append(
            {
                "ts": r.get("t"),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
            }
        )
    bars.sort(key=lambda b: b["ts"] or 0)
    return bars


class PolygonDailyBars:
    def __init__(self, api_key: str, timeout: int = 20):
        if not api_key:
            raise RuntimeError("outcome tracking requires POLYGON_API_KEY")
        self._api_key = api_key
        self._timeout = timeout

    def fetch(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        url = (
            POLYGON_AGGS_URL.format(symbol=symbol, start=start, end=end)
            + f"?adjusted=true&sort=asc&apiKey={self._api_key}"
        )
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                return polygon_aggs_to_bars(json.loads(resp.read().decode()))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return []  # too recent / no data yet -> stay pending
            raise
