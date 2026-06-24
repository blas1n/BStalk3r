"""Intraday 1-minute bars from Polygon (free historical aggregates).

Free tier serves historical minute bars (rate-limited, 429-safe via
polygon_http). Returns regular-hours bars only (09:30–16:00 ET, DST-correct),
which is where the paper strategy actually trades.
"""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from src.polygon_http import get_json

POLYGON_MINUTE_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/{date}/{date}"
_ET = ZoneInfo("America/New_York")


class MinuteBarsProvider(Protocol):
    def fetch(self, symbol: str, date: str) -> list[dict[str, Any]]: ...


def _in_rth(ts: datetime) -> bool:
    local = ts.astimezone(_ET)
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60  # [09:30, 16:00) ET


def polygon_minutes_to_bars(payload: dict[str, Any], rth_only: bool = True) -> list[dict[str, Any]]:
    rows = payload.get("results") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("t") is None or r.get("c") is None:
            continue
        ts = datetime.fromtimestamp(r["t"] / 1000, UTC)
        if rth_only and not _in_rth(ts):
            continue
        out.append(
            {
                "ts": ts,
                "open": float(r.get("o", r["c"])),
                "high": float(r.get("h", r["c"])),
                "low": float(r.get("l", r["c"])),
                "close": float(r["c"]),
                "volume": float(r.get("v", 0) or 0),
            }
        )
    out.sort(key=lambda b: b["ts"])
    return out


class PolygonMinuteBars:
    def __init__(self, api_key: str, timeout: int = 25):
        if not api_key:
            raise RuntimeError("intraday replay requires POLYGON_API_KEY")
        self._api_key = api_key
        self._timeout = timeout

    def fetch(self, symbol: str, date: str) -> list[dict[str, Any]]:
        url = (
            POLYGON_MINUTE_URL.format(symbol=symbol, date=date)
            + f"?adjusted=true&sort=asc&limit=50000&apiKey={self._api_key}"
        )
        try:
            return polygon_minutes_to_bars(get_json(url, self._timeout))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return []
            raise
