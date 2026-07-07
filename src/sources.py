"""Snapshot sources — where the scanner's candidate universe comes from.

Two implementations behind one interface:
- WatchlistSource: snapshots for a fixed symbol list (Alpaca/IEX metrics).
- PolygonGainersSource: screens the *whole* US market for top gainers in a
  single Polygon call — the runner-discovery path.

The strict entry filters still live in `scanner`/`strategy`; a source only
decides *which* symbols are looked at and fills MarketSnapshot fields.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from src.models import MarketSnapshot
from src.polygon_http import get_json

POLYGON_GAINERS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"
POLYGON_GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date}"
_SESSION_MINUTES = 390  # 6.5h regular session, for the avg-minute-volume proxy
_ET = ZoneInfo("America/New_York")


def _weekdays_back(start: date, n: int) -> Iterator[date]:
    """Up to `n` weekday dates from `start` going backward (skips Sat/Sun).

    Weekends never have grouped data, so skipping them avoids wasted API calls
    that still burn the free tier's 5 req/min budget and trigger 429s.
    """
    day = start
    yielded = 0
    while yielded < n:
        if day.weekday() < 5:  # Mon=0 .. Fri=4
            yield day
            yielded += 1
        day -= timedelta(days=1)


class SnapshotSource(Protocol):
    def fetch(self) -> list[MarketSnapshot]: ...


class MarketDataLike(Protocol):
    def get_snapshots(self, symbols: list[str]) -> list[MarketSnapshot]: ...


class WatchlistSource:
    def __init__(self, market: MarketDataLike, symbols: list[str]):
        self._market = market
        self._symbols = symbols

    def fetch(self) -> list[MarketSnapshot]:
        return self._market.get_snapshots(self._symbols)


class PolygonGainersSource:
    """Top market gainers via Polygon's snapshot endpoint (one HTTP call).

    Free tier is 15-min delayed and 5 req/min — fine for dry-run research.
    """

    def __init__(self, api_key: str, top_n: int = 50, timeout: int = 15):
        if not api_key:
            raise RuntimeError("UNIVERSE_SOURCE=polygon requires POLYGON_API_KEY")
        self._api_key = api_key
        self._top_n = top_n
        self._timeout = timeout

    def fetch(self) -> list[MarketSnapshot]:
        return polygon_gainers_to_snapshots(self._fetch_raw(), top_n=self._top_n)

    def _fetch_raw(self) -> dict[str, Any]:
        url = f"{POLYGON_GAINERS_URL}?apiKey={self._api_key}"
        return get_json(url, self._timeout)


def polygon_gainers_to_snapshots(
    payload: dict[str, Any], top_n: int = 50, session_minutes: int = _SESSION_MINUTES
) -> list[MarketSnapshot]:
    tickers = payload.get("tickers") or []
    out: list[MarketSnapshot] = []
    for t in tickers[:top_n]:
        snap = _ticker_to_snapshot(t, session_minutes)
        if snap is not None:
            out.append(snap)
    return out


def _ticker_to_snapshot(t: dict[str, Any], session_minutes: int) -> MarketSnapshot | None:
    day = t.get("day") or {}
    prev = t.get("prevDay") or {}
    quote = t.get("lastQuote") or {}
    trade = t.get("lastTrade") or {}
    minute = t.get("min") or {}

    last_price = trade.get("p") or day.get("c") or minute.get("c")
    symbol = t.get("ticker")
    if not last_price or not symbol:
        return None

    ask = quote.get("P")  # Polygon: P = ask price, p = bid price
    bid = quote.get("p")
    spread_pct = ((ask - bid) / last_price * 100) if (ask and bid and ask > 0) else 0.0

    day_vol = day.get("v") or 0
    prev_vol = prev.get("v") or 0
    rvol = (day_vol / prev_vol) if prev_vol else 0.0

    last_min_vol = minute.get("v") or 0
    avg_min_vol = (day_vol / session_minutes) if day_vol else 0
    vol_accel = (last_min_vol / avg_min_vol) if avg_min_vol else 1.0

    return MarketSnapshot(
        symbol=symbol,
        last_price=float(last_price),
        day_change_pct=float(t.get("todaysChangePerc", 0.0)),
        rvol=float(rvol),
        volume_acceleration=float(vol_accel),
        spread_pct=float(spread_pct),
        bid_price=float(bid) if bid else None,
        ask_price=float(ask) if ask else None,
        float_shares=None,
        market_cap=None,
    )


@dataclass(frozen=True)
class ScreenBounds:
    """Coarse pre-filter applied while screening the whole market."""

    min_price: float
    max_price: float
    min_change_pct: float


class PolygonGroupedSource:
    """Whole-market screen via Polygon grouped daily bars (free tier).

    Two calls (latest session + the one before) screen all ~12k US tickers for
    runners. This is **end-of-day** data: it surfaces the *prior session's*
    runners, so it builds a next-session watchlist / research set — it is not
    intraday. For live intraday screening use PolygonGainersSource (paid plan).
    Results are cached per session-date so the realtime loop stays within the
    free 5 req/min limit.
    """

    def __init__(
        self,
        api_key: str,
        bounds: ScreenBounds,
        top_n: int = 50,
        timeout: int = 20,
        max_lookback_days: int = 10,
    ):
        if not api_key:
            raise RuntimeError("UNIVERSE_SOURCE=polygon requires POLYGON_API_KEY")
        self._api_key = api_key
        self._bounds = bounds
        self._top_n = top_n
        self._timeout = timeout
        self._max_lookback = max_lookback_days
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def fetch(self) -> list[MarketSnapshot]:
        today_date, today_rows = self._latest_session_with_data()
        if not today_rows:
            return []
        prev_rows = self._session_before(today_date)
        prev_by_symbol = {r["T"]: r for r in prev_rows if r.get("T")}
        return polygon_grouped_to_snapshots(
            today_rows, prev_by_symbol, self._bounds, self._top_n, session_date=today_date
        )

    def _latest_session_with_data(self) -> tuple[str, list[dict[str, Any]]]:
        for day in _weekdays_back(datetime.now(_ET).date(), self._max_lookback):
            rows = self._fetch_grouped(day.isoformat())
            if rows:
                return day.isoformat(), rows
        return "", []

    def _session_before(self, date_iso: str) -> list[dict[str, Any]]:
        start = datetime.fromisoformat(date_iso).date() - timedelta(days=1)
        for day in _weekdays_back(start, self._max_lookback):
            rows = self._fetch_grouped(day.isoformat())
            if rows:
                return rows
        return []

    def _fetch_grouped(self, date_iso: str) -> list[dict[str, Any]]:
        if date_iso not in self._cache:
            self._cache[date_iso] = self._fetch_raw(date_iso).get("results") or []
        return self._cache[date_iso]

    def fetch_grouped(self, date_iso: str) -> list[dict[str, Any]]:
        """Public grouped rows for a specific date (cached). [] if no data."""
        return self._fetch_grouped(date_iso)

    def prev_session_rows(self, date_iso: str) -> list[dict[str, Any]]:
        """Grouped rows for the most recent trading session before `date_iso`."""
        start = datetime.fromisoformat(date_iso).date() - timedelta(days=1)
        for day in _weekdays_back(start, self._max_lookback):
            rows = self._fetch_grouped(day.isoformat())
            if rows:
                return rows
        return []

    def _fetch_raw(self, date_iso: str) -> dict[str, Any]:
        # Free tier 403s on the current/too-recent day (no realtime entitlement)
        # and 404s on non-trading days; treat both as "no data" so the caller
        # steps back to an older completed session.
        url = POLYGON_GROUPED_URL.format(date=date_iso) + f"?adjusted=true&apiKey={self._api_key}"
        try:
            return get_json(url, self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return {}
            raise


def polygon_grouped_to_snapshots(
    today_rows: list[dict[str, Any]],
    prev_by_symbol: dict[str, dict[str, Any]],
    bounds: ScreenBounds,
    top_n: int = 50,
    session_date: str | None = None,
) -> list[MarketSnapshot]:
    """Map grouped daily bars to runner snapshots (day change vs prior close).

    No intraday quote/minute data in this feed -> spread_pct=0, vol_accel=1.
    `session_date` (YYYY-MM-DD) stamps each snapshot's timestamp so the data is
    accumulated under the *trading date it represents*, not the capture day.
    """
    stamp = datetime.fromisoformat(session_date) if session_date else None
    out: list[MarketSnapshot] = []
    for row in today_rows:
        symbol = row.get("T")
        close = row.get("c")
        if not symbol or not close:
            continue
        prev = prev_by_symbol.get(symbol)
        prev_close = prev.get("c") if prev else None
        if not prev_close:
            continue
        change_pct = (close - prev_close) / prev_close * 100
        if not (bounds.min_price <= close <= bounds.max_price):
            continue
        if change_pct < bounds.min_change_pct:
            continue
        prev_vol = (prev.get("v") if prev else 0) or 0
        rvol = ((row.get("v") or 0) / prev_vol) if prev_vol else 0.0
        out.append(
            MarketSnapshot(
                symbol=symbol,
                last_price=float(close),
                day_change_pct=float(change_pct),
                rvol=float(rvol),
                volume_acceleration=1.0,
                spread_pct=0.0,
                bid_price=None,
                ask_price=None,
                float_shares=None,
                market_cap=None,
                timestamp=stamp,
            )
        )
    out.sort(key=lambda s: s.day_change_pct, reverse=True)
    return out[:top_n]


def polygon_grouped_crossers(
    today_rows: list[dict[str, Any]],
    prev_by_symbol: dict[str, dict[str, Any]],
    min_price: float,
    max_price: float,
    entry_trigger: float,
) -> list[dict[str, Any]]:
    """Every stock whose intraday HIGH crossed `entry_trigger` % vs prior close.

    This is the *survivorship-inclusive* universe a live scanner would fire on —
    unlike the close-based screener it keeps "fizzles" (popped then faded).
    Band filter is on the entry price estimate (prev_close × (1+trigger)), i.e.
    what you'd actually pay, so fizzles that closed cheap are still included.
    """
    out: list[dict[str, Any]] = []
    for row in today_rows:
        symbol = row.get("T")
        close = row.get("c")
        high = row.get("h")
        if not symbol or not close or not high:
            continue
        prev = prev_by_symbol.get(symbol)
        prev_close = prev.get("c") if prev else None
        if not prev_close:
            continue
        chg_high = (high - prev_close) / prev_close * 100
        if chg_high < entry_trigger:
            continue
        entry_est = prev_close * (1 + entry_trigger / 100)
        if not (min_price <= entry_est <= max_price):
            continue
        chg_close = (close - prev_close) / prev_close * 100
        out.append(
            {
                "symbol": symbol,
                "prev_close": float(prev_close),
                "close": float(close),
                "high": float(high),
                "chg_high": chg_high,
                "chg_close": chg_close,
                "is_fizzle": chg_close < entry_trigger,
            }
        )
    return out
