"""Ticker identity breaks (#58): a reused ticker stitches two companies into one
series. The rule needs positive evidence on both sides — a gap in the cached
series AND the current holder's Polygon ticker event (it took the ticker inside
that gap). A gap alone (halt) or an event alone never splits."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
import src.identity as identity
from src.identity import (
    IdentityBreak,
    PolygonIdentitySource,
    find_breaks,
    gap_ends,
    split_identities,
)

# 20 sessions; the ticker has no bar at sessions 8..13 (6 missing)
_S = [f"2026-03-{d:02d}" for d in range(2, 32)][:20]
_DS = _S[:8] + _S[14:]


def test_gap_ends_marks_the_first_bar_after_a_long_gap():
    assert gap_ends(_DS, _S) == [8]  # _DS[8] == _S[14]


def test_gap_ends_ignores_short_gaps():
    ds = _S[:8] + _S[11:]  # 3 missing sessions: an ordinary thin-trading hole
    assert gap_ends(ds, _S) == []


def test_reuse_inside_the_gap_is_a_break():
    got = find_breaks("PARA", _DS, _S, acquired=[_S[14]])
    assert got == [IdentityBreak("PARA", last_old=_S[7], first_new=_S[14], acquired=_S[14])]


def test_event_a_few_days_after_the_first_new_bar_snaps_to_the_gap():
    # Polygon's event date can trail the first print under the new holder
    got = find_breaks("PARA", _DS, _S, acquired=[_S[16]])
    assert [(b.last_old, b.first_new) for b in got] == [(_S[7], _S[14])]


def test_halt_gap_without_an_identity_change_is_not_split():
    # holder took the ticker long before the cache starts (IPO), or no event at all
    assert find_breaks("HLT", _DS, _S, acquired=["2001-05-01"]) == []
    assert find_breaks("HLT", _DS, _S, acquired=[]) == []


def test_event_without_a_gap_is_not_split():
    # event inside continuous history: no gap to corroborate it -> leave the series
    assert find_breaks("XYZ", _S, _S, acquired=[_S[5]]) == []


def test_event_far_after_the_gap_is_not_split():
    assert find_breaks("PARA", _DS, _S, acquired=["2026-04-30"]) == []


def test_split_identities_renames_the_earlier_company():
    panel = {d: {"PARA": {"close": 1.0, "dollar_vol": 1.0}, "AAA": {"close": 2.0}} for d in _DS}
    b = IdentityBreak("PARA", _S[7], _S[14], _S[14])
    out = split_identities(panel, [b])
    assert b.old_name == f"PARA<{_S[14]}"
    for d in _DS:
        assert "AAA" in out[d]
        if d < _S[14]:
            assert set(out[d]) == {"AAA", b.old_name}
        else:
            assert set(out[d]) == {"AAA", "PARA"}
    assert "PARA" in panel[_DS[0]]  # input untouched


def test_split_identities_twice_reused_ticker_gets_three_series():
    ds = _S[:4] + _S[10:12] + _S[18:]
    panel = {d: {"X": {"close": 1.0}} for d in ds}
    b1 = IdentityBreak("X", _S[3], _S[10], _S[10])
    b2 = IdentityBreak("X", _S[11], _S[18], _S[18])
    out = split_identities(panel, [b2, b1])
    names = [next(iter(out[d])) for d in ds]
    assert names == [b1.old_name] * 4 + [b2.old_name] * 2 + ["X"] * 2


# --- Polygon ticker events --------------------------------------------------------


def _events(*pairs):
    return {
        "status": "OK",
        "results": {
            "events": [
                {"type": "ticker_change", "date": d, "ticker_change": {"ticker": t}}
                for t, d in pairs
            ]
        },
    }


class _Http:
    def __init__(self, answers):
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url, timeout=20):
        self.urls.append(url)
        a = self.answers[len(self.urls) - 1]
        if isinstance(a, int):
            raise urllib.error.HTTPError(url, a, "x", {}, io.BytesIO(b"{}"))
        return a


def _source(monkeypatch, tmp_path, answers, today="2026-09-29", **kw):
    from datetime import date

    http = _Http(answers)
    monkeypatch.setattr(identity, "get_json", http)
    sleeps: list[float] = []
    monkeypatch.setattr(identity.time, "sleep", sleeps.append)
    src = PolygonIdentitySource(
        "PK",
        cache_path=str(tmp_path / "id.db"),
        today=date.fromisoformat(today),
        **kw,
    )
    return src, http, sleeps


def test_polygon_source_returns_dates_the_holder_took_this_ticker(monkeypatch, tmp_path):
    src, http, _ = _source(
        monkeypatch, tmp_path, [_events(("PARA", "2026-08-07"), ("BNZI", "2026-05-08"))]
    )
    assert src.acquisitions("PARA", known_after="2026-08-07") == ["2026-08-07"]
    assert "/vX/reference/tickers/PARA/events" in http.urls[0]
    assert "apiKey=PK" in http.urls[0]


def test_polygon_source_404_means_no_current_holder(monkeypatch, tmp_path):
    src, _, _ = _source(monkeypatch, tmp_path, [404])
    assert src.acquisitions("VIAC", known_after="2026-01-02") == []


def test_polygon_source_other_http_errors_propagate(monkeypatch, tmp_path):
    src, _, _ = _source(monkeypatch, tmp_path, [500])
    with pytest.raises(urllib.error.HTTPError):
        src.acquisitions("PARA", known_after="2026-01-02")


def test_polygon_source_serves_a_fetch_newer_than_the_gap_from_disk(monkeypatch, tmp_path):
    src, http, _ = _source(monkeypatch, tmp_path, [_events(("JAN", "2026-03-19"))])
    assert src.acquisitions("JAN", known_after="2026-03-20") == ["2026-03-19"]
    again = PolygonIdentitySource("PK", cache_path=str(tmp_path / "id.db"))
    assert again.acquisitions("JAN", known_after="2026-03-20") == ["2026-03-19"]
    assert len(http.urls) == 1


def test_polygon_source_refetches_when_the_gap_is_newer_than_the_fetch(monkeypatch, tmp_path):
    src, http, _ = _source(
        monkeypatch,
        tmp_path,
        [_events(("JAN", "2020-01-02")), _events(("JAN", "2026-09-28"))],
        today="2026-09-01",
    )
    assert src.acquisitions("JAN", known_after="2026-08-01") == ["2020-01-02"]
    later = PolygonIdentitySource(
        "PK", cache_path=str(tmp_path / "id.db"), today=__import__("datetime").date(2026, 9, 30)
    )
    assert later.acquisitions("JAN", known_after="2026-09-28") == ["2026-09-28"]
    assert len(http.urls) == 2


def test_polygon_source_quotes_the_ticker_and_throttles_live_fetches(monkeypatch, tmp_path):
    src, http, sleeps = _source(monkeypatch, tmp_path, [_events(), _events()], throttle_sec=13)
    src.acquisitions("BRK.B", known_after="2026-01-02")
    src.acquisitions("A/B", known_after="2026-01-02")
    assert "/tickers/A%2FB/events" in http.urls[1]
    assert sleeps == [13]  # between live fetches, not before the first


def test_polygon_source_requires_a_key():
    with pytest.raises(RuntimeError):
        PolygonIdentitySource("")


def test_polygon_source_cache_row_is_json(monkeypatch, tmp_path):
    import sqlite3

    src, _, _ = _source(monkeypatch, tmp_path, [_events(("PARA", "2026-08-07"))])
    src.acquisitions("PARA", known_after="2026-08-07")
    row = sqlite3.connect(tmp_path / "id.db").execute("SELECT * FROM ticker_events").fetchone()
    assert row[0] == "PARA" and json.loads(row[1]) == ["2026-08-07"]
