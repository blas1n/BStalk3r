"""Snapshot sources: Polygon gainers mapping + watchlist delegation.

The HTTP call is never made in tests — `polygon_gainers_to_snapshots` is pure
and `PolygonGainersSource._fetch_raw` is monkeypatched with a captured payload.
"""

from __future__ import annotations

from datetime import date

from src.models import MarketSnapshot
from src.sources import (
    PolygonGainersSource,
    PolygonGroupedSource,
    ScreenBounds,
    WatchlistSource,
    _weekdays_back,
    polygon_gainers_to_snapshots,
    polygon_grouped_to_snapshots,
)

BOUNDS = ScreenBounds(min_price=1.0, max_price=50.0, min_change_pct=5.0)


def test_weekdays_back_skips_weekends():
    # 2026-06-24 is a Wednesday; going back should skip Sat 06-20 / Sun 06-21
    out = list(_weekdays_back(date(2026, 6, 24), 5))
    assert out == [
        date(2026, 6, 24),  # Wed
        date(2026, 6, 23),  # Tue
        date(2026, 6, 22),  # Mon
        date(2026, 6, 19),  # Fri (skipped Sun 21, Sat 20)
        date(2026, 6, 18),  # Thu
    ]


def test_weekdays_back_starting_on_weekend():
    # start Sat 2026-06-20 -> first yielded is Fri 06-19
    out = list(_weekdays_back(date(2026, 6, 20), 2))
    assert out == [date(2026, 6, 19), date(2026, 6, 18)]


# Trimmed real-shape Polygon /v2/snapshot/.../gainers payload.
SAMPLE = {
    "status": "OK",
    "tickers": [
        {
            "ticker": "RUNR",
            "todaysChangePerc": 22.5,
            "day": {"o": 7.0, "h": 9.0, "l": 6.8, "c": 8.5, "v": 40_000_000, "vw": 8.1},
            "lastQuote": {"P": 8.52, "p": 8.48, "S": 3, "s": 2},
            "lastTrade": {"p": 8.50},
            "min": {"av": 40_000_000, "o": 8.4, "h": 8.6, "l": 8.4, "c": 8.5, "v": 500_000},
            "prevDay": {"o": 7.0, "h": 7.1, "l": 6.9, "c": 6.94, "v": 4_000_000, "vw": 7.0},
        },
        {
            "ticker": "NOQT",  # no quote -> spread defaults to 0, still mapped
            "todaysChangePerc": 11.0,
            "day": {"c": 3.0, "v": 2_000_000},
            "lastTrade": {"p": 3.05},
            "prevDay": {"c": 2.75, "v": 1_000_000},
        },
    ],
}


def test_maps_core_fields():
    snaps = polygon_gainers_to_snapshots(SAMPLE)
    by = {s.symbol: s for s in snaps}
    runr = by["RUNR"]
    assert isinstance(runr, MarketSnapshot)
    assert runr.last_price == 8.50
    assert runr.day_change_pct == 22.5
    assert runr.ask_price == 8.52
    assert runr.bid_price == 8.48
    # spread = (8.52-8.48)/8.50 * 100
    assert abs(runr.spread_pct - 0.470588) < 1e-3
    # rvol proxy = today vol / prev-day vol = 40M / 4M
    assert abs(runr.rvol - 10.0) < 1e-6
    # float not available from this feed
    assert runr.float_shares is None


def test_volume_acceleration_proxy():
    runr = polygon_gainers_to_snapshots(SAMPLE)[0]
    # last-minute vol 500k vs avg minute = 40M / 390 ~ 102.6k -> ~4.87x
    assert runr.volume_acceleration > 3.0


def test_missing_quote_does_not_crash():
    by = {s.symbol: s for s in polygon_gainers_to_snapshots(SAMPLE)}
    noqt = by["NOQT"]
    assert noqt.spread_pct == 0.0
    assert noqt.ask_price is None
    assert noqt.last_price == 3.05


def test_top_n_caps_results():
    snaps = polygon_gainers_to_snapshots(SAMPLE, top_n=1)
    assert [s.symbol for s in snaps] == ["RUNR"]


def test_empty_payload_is_safe():
    assert polygon_gainers_to_snapshots({"status": "OK"}) == []
    assert polygon_gainers_to_snapshots({"tickers": None}) == []


def test_polygon_source_fetch_uses_raw(monkeypatch):
    src = PolygonGainersSource(api_key="k", top_n=50)
    monkeypatch.setattr(src, "_fetch_raw", lambda: SAMPLE)
    syms = [s.symbol for s in src.fetch()]
    assert syms == ["RUNR", "NOQT"]


class _FakeMarket:
    def __init__(self):
        self.calls = []

    def get_snapshots(self, symbols):
        self.calls.append(symbols)
        return [MarketSnapshot(s, 10.0, 6.0, 9.0, 4.0, 0.3) for s in symbols]


def test_watchlist_source_delegates_to_market():
    market = _FakeMarket()
    src = WatchlistSource(market, ["AAA", "BBB"])
    out = src.fetch()
    assert [s.symbol for s in out] == ["AAA", "BBB"]
    assert market.calls == [["AAA", "BBB"]]


# ---- grouped (free EOD) screener ------------------------------------------

TODAY = [
    {"T": "RUNR", "c": 8.5, "v": 40_000_000},  # +22.5% vs prev, in window
    {"T": "BLAZ", "c": 12.0, "v": 30_000_000},  # +50% vs prev, stronger
    {"T": "BIG", "c": 200.0, "v": 1_000_000},  # price out of window
    {"T": "TINY", "c": 0.5, "v": 1_000_000},  # price below floor
    {"T": "FLAT", "c": 10.0, "v": 1_000_000},  # change too small
    {"T": "NOPREV", "c": 9.0, "v": 1_000_000},  # missing in prev -> skip
]
PREV = {
    "RUNR": {"T": "RUNR", "c": 6.9388, "v": 4_000_000},
    "BLAZ": {"T": "BLAZ", "c": 8.0, "v": 3_000_000},
    "BIG": {"T": "BIG", "c": 150.0, "v": 900_000},
    "TINY": {"T": "TINY", "c": 0.4, "v": 900_000},
    "FLAT": {"T": "FLAT", "c": 9.9, "v": 900_000},
}


def test_grouped_screens_and_ranks_by_change():
    snaps = polygon_grouped_to_snapshots(TODAY, PREV, BOUNDS)
    # BLAZ (+50%) ranked above RUNR (+22.5%); others filtered out
    assert [s.symbol for s in snaps] == ["BLAZ", "RUNR"]


def test_grouped_computes_change_and_rvol():
    snaps = polygon_grouped_to_snapshots(TODAY, PREV, BOUNDS)
    runr = next(s for s in snaps if s.symbol == "RUNR")
    assert abs(runr.day_change_pct - 22.5) < 0.1
    assert abs(runr.rvol - 10.0) < 1e-6  # 40M / 4M
    assert runr.spread_pct == 0.0  # no intraday quote in grouped feed
    assert runr.volume_acceleration == 1.0


def test_grouped_top_n():
    assert len(polygon_grouped_to_snapshots(TODAY, PREV, BOUNDS, top_n=1)) == 1


def test_grouped_stamps_session_date():
    snaps = polygon_grouped_to_snapshots(TODAY, PREV, BOUNDS, session_date="2026-06-09")
    assert all(s.timestamp is not None for s in snaps)
    assert snaps[0].timestamp.date().isoformat() == "2026-06-09"


def test_grouped_source_fetch_wires_two_sessions(monkeypatch):
    src = PolygonGroupedSource(api_key="k", bounds=BOUNDS, top_n=50)
    monkeypatch.setattr(src, "_latest_session_with_data", lambda: ("2026-06-09", TODAY))
    monkeypatch.setattr(src, "_session_before", lambda d: list(PREV.values()))
    out = src.fetch()
    assert [s.symbol for s in out] == ["BLAZ", "RUNR"]
    # source stamps the data's session date for durable accumulation
    assert out[0].timestamp.date().isoformat() == "2026-06-09"


def test_grouped_source_requires_key():
    import pytest

    with pytest.raises(RuntimeError):
        PolygonGroupedSource(api_key="", bounds=BOUNDS)


def test_latest_session_steps_back_to_available_data(monkeypatch):
    from datetime import date

    src = PolygonGroupedSource(api_key="k", bounds=BOUNDS)
    # data exists only for 2026-07-02; 07-06 (Mon), 07-03 (Fri) return nothing
    monkeypatch.setattr(src, "_fetch_grouped", lambda d: [{"T": "X"}] if d == "2026-07-02" else [])
    # from a Monday, lag 0 -> steps Mon,Fri,Thu... until data at 2026-07-02 (Thu)
    assert src.latest_session(lag_days=0, _today=date(2026, 7, 6)) == "2026-07-02"


def test_latest_session_honors_lag_days(monkeypatch):
    from datetime import date

    src = PolygonGroupedSource(api_key="k", bounds=BOUNDS)
    monkeypatch.setattr(src, "_fetch_grouped", lambda d: [{"T": "X"}])  # every day has data
    # lag 2 from Fri 2026-07-03 -> start at Wed 2026-07-01 (latest with data)
    assert src.latest_session(lag_days=2, _today=date(2026, 7, 3)) == "2026-07-01"


def test_latest_session_empty_when_no_data(monkeypatch):
    from datetime import date

    src = PolygonGroupedSource(api_key="k", bounds=BOUNDS)
    monkeypatch.setattr(src, "_fetch_grouped", lambda d: [])
    assert src.latest_session(_today=date(2026, 7, 6)) == ""
