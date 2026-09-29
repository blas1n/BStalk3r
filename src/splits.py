"""Split adjustment of grouped-cache closes from corporate-action events (#47).

Events come from Polygon `/v3/reference/splits` (ticker, execution_date,
split_from, split_to), fetched once per date range and cached on disk.

Why the adjustment still has to *locate* each split in the cache: grouped rows
are fetched with `adjusted=true`, one session at a time, so every row is adjusted
for the splits Polygon knew **when that row was fetched**. Two consequences,
both measured on the 2024-07..2026-09 cache:

* Rows backfilled after a split are already in post-split units — there is no
  jump, and applying the event again would restate history twice (e.g. KORU's
  2025-02-10 1:10 reverse split). Most events in the cache look like this.
* Rows fetched day by day before a split is known are raw, so the jump appears
  at the first row fetched after Polygon registered it: on the execution date or
  up to `WINDOW` sessions before it (MUU/KORU 1->20 on 2026-07-15 jump on 07-14).

So for each event, the boundary is the session in that window whose close ratio
is closest (in log terms) to the event's price factor, accepted only if it is
closer to the factor than to "no move". Every earlier close of the symbol is
multiplied by the factor. The event supplies the ratio, so a split that lands on
a big real move is still caught, and a real spike with no event is never touched.
Dollar volume is left raw: close x volume is split-invariant.

Absolute-price filters must not read the restated close: a later 1-for-10
reverse split lifts a $2 print to $20, inside the $5-1000 band (#55). So a
record dated before one of its symbol's split events also keeps `traded_close`,
the close it printed at: the adjusted close divided by each event executing after that
session which the adjusted series reflects — every event located in the cache,
and every event the symbol traded across (cached sessions within a week on both
sides) without a jump, which means the cache had already restated it. An event
after the last cached session, or in a gap of the symbol's history (ticker
reuse: PARA), gives no evidence of restatement and is left alone.
Limit: grouped rows carry no fetch time, so a split after `end` + lookahead
that a backfilled row already reflects cannot be undone; windows ending at the
cache's latest session are exact.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from src.polygon_http import get_json

POLYGON_SPLITS_URL = "https://api.polygon.io/v3/reference/splits"
WINDOW = 2  # sessions before the execution date an as-of-adjusted boundary may land
_LOOKAHEAD_DAYS = 7  # events just after `end` can shift the last cached rows

Panel = dict[str, dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class SplitEvent:
    ticker: str
    execution_date: str  # first session trading in post-split units (YYYY-MM-DD)
    split_from: float
    split_to: float

    @property
    def price_factor(self) -> float:
        """Multiplier that restates a pre-split price in post-split units."""
        return self.split_from / self.split_to


class SplitSource(Protocol):
    def fetch_range(self, start: str, end: str) -> list[SplitEvent]: ...


def split_adjust_panel(panel: Panel, events: list[SplitEvent]) -> Panel:
    """Copy of a {date: {symbol: {close, dollar_vol, ...}}} panel with each
    symbol's closes back-adjusted for the split events located in the cache,
    plus `traded_close` where the as-traded close differs (#55).
    Consecutive splits compound; other fields are copied unchanged."""
    return adjust_panel(panel, events)[0]


def adjust_panel(panel: Panel, events: list[SplitEvent]) -> tuple[Panel, int]:
    """`split_adjust_panel` plus the number of events actually applied."""
    by_sym: dict[str, list[SplitEvent]] = {}
    for ev in events:
        if ev.split_from > 0 and ev.split_to > 0 and ev.price_factor != 1.0:
            by_sym.setdefault(ev.ticker, []).append(ev)
    dates = sorted(panel)
    out: Panel = {d: {s: dict(rec) for s, rec in panel[d].items()} for d in dates}
    applied = 0
    for sym, evs in by_sym.items():
        ds = [d for d in dates if sym in panel[d]]
        closes = [panel[d][sym]["close"] for d in ds]
        factors = [1.0] * len(ds)
        restated = [1.0] * len(ds)  # adjusted / as-traded
        for ev in evs:
            k = _locate_boundary(ds, closes, ev)
            if k is not None:
                applied += 1
                for i in range(k):
                    factors[i] *= ev.price_factor
            if k is not None or _straddles(ds, ev.execution_date):
                for i, d in enumerate(ds):
                    if d < ev.execution_date:
                        restated[i] *= ev.price_factor
        for d, c, f, r in zip(ds, closes, factors, restated, strict=True):
            if f != 1.0:
                out[d][sym]["close"] = c * f
            if r != 1.0:
                out[d][sym]["traded_close"] = c * f / r
    return out, applied


def _locate_boundary(ds: list[str], closes: list[float], ev: SplitEvent) -> int | None:
    """Index of the first cached bar in post-split units, or None when the cache
    shows no split-sized jump near the execution date (already adjusted)."""
    if len(ds) < 2:
        return None
    i = _bisect_left(ds, ev.execution_date)
    if i == 0 or (i == len(ds) and ds[-1] < _minus_days(ev.execution_date, 7)):
        return None
    target = math.log(ev.price_factor)
    best: tuple[float, int] | None = None
    for k in range(max(1, i - WINDOW), min(len(ds) - 1, i) + 1):
        prev, cur = closes[k - 1], closes[k]
        if prev <= 0 or cur <= 0:
            continue
        x = math.log(cur / prev)
        dist = abs(x - target)
        if dist < abs(x) and (best is None or dist < best[0]):
            best = (dist, k)
    return best[1] if best else None


def _straddles(ds: list[str], execution_date: str) -> bool:
    """The symbol has cached sessions within a week on both sides of the
    execution date, so "no jump there" is evidence the earlier rows were
    restated. A gap (ticker reuse, halt) gives no such evidence."""
    i = _bisect_left(ds, execution_date)
    return (
        0 < i < len(ds)
        and ds[i - 1] >= _minus_days(execution_date, 7)
        and ds[i] <= _minus_days(execution_date, -7)
    )


def _bisect_left(ds: list[str], key: str) -> int:
    lo, hi = 0, len(ds)
    while lo < hi:
        mid = (lo + hi) // 2
        if ds[mid] < key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _minus_days(iso: str, n: int) -> str:
    return (datetime.fromisoformat(iso).date() - timedelta(days=n)).isoformat()


class PolygonSplitSource:
    """Split events from Polygon `/v3/reference/splits`, cached per date range.

    A fetched range is served from disk afterwards if it was fully in the past
    when fetched; a range still open then (splits are announced ahead, so it may
    grow) is served from disk the same day and refetched on a later day.
    HTTP errors propagate: a backtest must not silently run on raw closes."""

    def __init__(
        self,
        api_key: str,
        cache_path: str = "",
        timeout: int = 20,
        today: date | None = None,
    ):
        if not api_key:
            raise RuntimeError("split adjustment requires POLYGON_API_KEY")
        self._api_key = api_key
        self._timeout = timeout
        self._today = today
        self._disk: sqlite3.Connection | None = None
        if cache_path:
            if cache_path != ":memory:":
                Path(cache_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._disk = sqlite3.connect(cache_path)
            self._disk.execute(
                "CREATE TABLE IF NOT EXISTS splits (ticker TEXT NOT NULL, "
                "execution_date TEXT NOT NULL, split_from REAL NOT NULL, "
                "split_to REAL NOT NULL, PRIMARY KEY (ticker, execution_date))"
            )
            self._disk.execute(
                "CREATE TABLE IF NOT EXISTS splits_coverage (start TEXT NOT NULL, "
                "end TEXT NOT NULL, fetched_on TEXT NOT NULL)"
            )
            self._disk.commit()

    def fetch_range(self, start: str, end: str) -> list[SplitEvent]:
        """Split events with execution_date in [start, end + a week]; the week
        catches splits whose as-of boundary lands on the last cached sessions."""
        hi = (datetime.fromisoformat(end).date() + timedelta(days=_LOOKAHEAD_DAYS)).isoformat()
        if self._disk is not None and self._covered(start, hi):
            return self._read(start, hi)
        events = self._fetch_raw(start, hi)
        if self._disk is not None:
            self._disk.executemany(
                "INSERT OR REPLACE INTO splits VALUES (?,?,?,?)",
                [(e.ticker, e.execution_date, e.split_from, e.split_to) for e in events],
            )
            self._disk.execute(
                "INSERT INTO splits_coverage VALUES (?,?,?)",
                (start, hi, self._now().isoformat()),
            )
            self._disk.commit()
        return events

    def _now(self) -> date:
        return self._today or date.today()

    def _covered(self, start: str, end: str) -> bool:
        assert self._disk is not None
        row = self._disk.execute(
            "SELECT 1 FROM splits_coverage WHERE start<=? AND end>=? "
            "AND (fetched_on>? OR fetched_on>=?)",
            (start, end, end, self._now().isoformat()),
        ).fetchone()
        return row is not None

    def _read(self, start: str, end: str) -> list[SplitEvent]:
        assert self._disk is not None
        rows = self._disk.execute(
            "SELECT ticker, execution_date, split_from, split_to FROM splits "
            "WHERE execution_date BETWEEN ? AND ? ORDER BY execution_date, ticker",
            (start, end),
        ).fetchall()
        return [SplitEvent(t, d, f, s) for t, d, f, s in rows]

    def _fetch_raw(self, start: str, end: str) -> list[SplitEvent]:
        url = (
            f"{POLYGON_SPLITS_URL}?execution_date.gte={start}&execution_date.lte={end}"
            f"&order=asc&sort=execution_date&limit=1000&apiKey={self._api_key}"
        )
        out: list[SplitEvent] = []
        pages = 0
        while url and pages < 100:  # safety cap (~100k events)
            data = get_json(url, self._timeout)
            for r in data.get("results") or []:
                ev = _parse(r)
                if ev is not None:
                    out.append(ev)
            nxt = data.get("next_url")
            url = f"{nxt}&apiKey={self._api_key}" if nxt else ""
            pages += 1
        return out


def _parse(r: dict[str, Any]) -> SplitEvent | None:
    try:
        sf, st = float(r["split_from"]), float(r["split_to"])
        t, d = str(r["ticker"]), str(r["execution_date"])
    except (KeyError, TypeError, ValueError):
        return None
    if not t or not d or sf <= 0 or st <= 0:
        return None
    return SplitEvent(t, d, sf, st)
