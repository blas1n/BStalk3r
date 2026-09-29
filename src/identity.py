"""Ticker identity breaks (#58): a reused ticker stitches two companies into one
series.

The grouped cache is keyed by ticker, so when an exchange hands a retired ticker
to a different company (Paramount's `PARA` rows end 2025-08-06; Banzai trades as
`PARA` from 2026-08-07) the per-symbol series joins the two. Backtests then book
fake returns across the join and compute RSI-2/SMA200 over two companies.

Rule — a break needs positive evidence on both sides:
  1. a gap in the ticker's cached series of at least `MIN_GAP_SESSIONS` panel
     sessions, and
  2. a Polygon ticker event showing the ticker's *current* holder took this ticker
     inside that gap: after the last bar before the gap and no later than
     `SNAP_DAYS` calendar days after the first bar after it (the event date can
     trail the first print).
A gap alone (a halt, thin trading) or an event alone (a rename into a ticker
nobody else used, or a misdated event inside continuous history) never splits.
The rows before the break are renamed `"{ticker}<{first_new}"`, so the current
holder keeps the plain ticker, as live sees it.

Limits: reuse with fewer than `MIN_GAP_SESSIONS` missing sessions is not
detected; only tickers with a qualifying gap are looked up (API budget).
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from src.polygon_http import get_json

MIN_GAP_SESSIONS = 5
SNAP_DAYS = 7
POLYGON_EVENTS_URL = "https://api.polygon.io/vX/reference/tickers/{ticker}/events"

Panel = dict[str, dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class IdentityBreak:
    ticker: str
    last_old: str  # last session of the earlier company
    first_new: str  # first session of the current holder
    acquired: str  # Polygon ticker_change date

    @property
    def old_name(self) -> str:
        """Symbol the earlier company's rows are renamed to."""
        return f"{self.ticker}<{self.first_new}"


class IdentitySource(Protocol):
    def acquisitions(self, ticker: str, known_after: str) -> list[str]:
        """Dates on which `ticker`'s current holder took this ticker. The answer
        must reflect events up to at least `known_after`."""
        ...


def gap_ends(ds: list[str], sessions: list[str], min_gap: int = MIN_GAP_SESSIONS) -> list[int]:
    """Indices k into `ds` (the ticker's cached dates) where at least `min_gap`
    panel `sessions` are missing between ds[k-1] and ds[k]."""
    pos = {d: i for i, d in enumerate(sessions)}
    return [k for k in range(1, len(ds)) if pos[ds[k]] - pos[ds[k - 1]] - 1 >= min_gap]


def find_breaks(
    ticker: str, ds: list[str], sessions: list[str], acquired: list[str]
) -> list[IdentityBreak]:
    """Identity breaks in one ticker's series: gaps whose span holds a date on
    which the current holder took the ticker (see module rule)."""
    out: list[IdentityBreak] = []
    for k in gap_ends(ds, sessions):
        last_old, first_new = ds[k - 1], ds[k]
        hi = _plus_days(first_new, SNAP_DAYS)
        hits = [a for a in acquired if last_old < a <= hi]
        if hits:
            out.append(IdentityBreak(ticker, last_old, first_new, min(hits)))
    return out


def split_identities(panel: Panel, breaks: list[IdentityBreak]) -> Panel:
    """Copy of `panel` with each broken ticker's rows before a break renamed to
    that break's `old_name`; the current holder keeps the plain ticker."""
    by_sym: dict[str, list[IdentityBreak]] = {}
    for b in breaks:
        by_sym.setdefault(b.ticker, []).append(b)
    for bs in by_sym.values():
        bs.sort(key=lambda b: b.first_new)
    out: Panel = {}
    for d, day in panel.items():
        row: dict[str, dict[str, Any]] = {}
        for sym, rec in day.items():
            name = sym
            for b in by_sym.get(sym, ()):
                if d < b.first_new:
                    name = b.old_name
                    break
            row[name] = dict(rec)
        out[d] = row
    return out


def identity_breaks(
    panel: Panel, source: IdentitySource, min_dollar_vol: float = 0.0
) -> tuple[list[IdentityBreak], int]:
    """All identity breaks in `panel`, looking up only tickers with a qualifying
    gap and at least one bar at or above `min_dollar_vol`. Returns (breaks,
    number of tickers looked up)."""
    sessions = sorted(panel)
    dates: dict[str, list[str]] = {}
    liquid: set[str] = set()
    for d in sessions:
        for sym, rec in panel[d].items():
            dates.setdefault(sym, []).append(d)
            if float(rec.get("dollar_vol", 0.0) or 0.0) >= min_dollar_vol:
                liquid.add(sym)
    breaks: list[IdentityBreak] = []
    looked = 0
    for sym in sorted(liquid):
        ds = dates[sym]
        ends = gap_ends(ds, sessions)
        if not ends:
            continue
        looked += 1
        acquired = source.acquisitions(sym, known_after=ds[ends[-1]])
        breaks.extend(find_breaks(sym, ds, sessions, acquired))
    return breaks, looked


def _plus_days(iso: str, n: int) -> str:
    return (datetime.fromisoformat(iso).date() + timedelta(days=n)).isoformat()


class PolygonIdentitySource:
    """Current-holder ticker events from Polygon `/vX/reference/tickers/{t}/events`,
    cached per ticker. A cached answer serves a gap that ended at least
    `SNAP_DAYS` before it was fetched (the event would have been recorded), or
    any gap on the day it was fetched. 404 = no current holder (no events).
    Other HTTP errors propagate: a backtest must not silently skip the check."""

    def __init__(
        self,
        api_key: str,
        cache_path: str = "",
        timeout: int = 20,
        throttle_sec: float = 0.0,
        today: date | None = None,
    ):
        if not api_key:
            raise RuntimeError("ticker identity check requires POLYGON_API_KEY")
        self._api_key = api_key
        self._timeout = timeout
        self._throttle = throttle_sec
        self._today = today
        self._fetched = 0
        self._disk: sqlite3.Connection | None = None
        if cache_path:
            if cache_path != ":memory:":
                Path(cache_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._disk = sqlite3.connect(cache_path)
            self._disk.execute(
                "CREATE TABLE IF NOT EXISTS ticker_events (ticker TEXT PRIMARY KEY, "
                "acquired_json TEXT NOT NULL, fetched_on TEXT NOT NULL)"
            )
            self._disk.commit()

    def acquisitions(self, ticker: str, known_after: str) -> list[str]:
        today = (self._today or date.today()).isoformat()
        if self._disk is not None:
            row = self._disk.execute(
                "SELECT acquired_json, fetched_on FROM ticker_events WHERE ticker=?", (ticker,)
            ).fetchone()
            if row and (row[1] >= _plus_days(known_after, SNAP_DAYS) or row[1] >= today):
                return list(json.loads(row[0]))
        got = self._fetch(ticker)
        if self._disk is not None:
            self._disk.execute(
                "INSERT OR REPLACE INTO ticker_events VALUES (?,?,?)",
                (ticker, json.dumps(got), today),
            )
            self._disk.commit()
        return got

    def _fetch(self, ticker: str) -> list[str]:
        if self._fetched and self._throttle:
            time.sleep(self._throttle)
        self._fetched += 1
        url = (
            POLYGON_EVENTS_URL.format(ticker=urllib.parse.quote(ticker, safe=""))
            + f"?types=ticker_change&apiKey={self._api_key}"
        )
        try:
            data = get_json(url, self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        events = (data.get("results") or {}).get("events") or []
        out = []
        for ev in events:
            tc = ev.get("ticker_change") or {}
            if ev.get("type") == "ticker_change" and tc.get("ticker") == ticker and ev.get("date"):
                out.append(str(ev["date"]))
        return sorted(set(out))
