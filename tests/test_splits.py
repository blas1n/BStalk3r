"""Split adjustment of grouped-cache closes from real corporate-action events (#47).

The grouped cache is fetched with `adjusted=true` one session at a time, so each
row is adjusted for the splits Polygon knew *when it was fetched*. A split shows
up as a one-day jump only if the cache holds pre-split rows fetched before the
split was known; the jump lands on the execution date or a session or two before
it. Rows backfilled after a split are already adjusted and must not be touched
again. The adjustment is driven by the event (ratio + date), never by the size
of a price move alone.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import date, timedelta

import pytest
import src.splits as splits_mod
from src.mean_reversion import mean_reversion_trades
from src.splits import PolygonSplitSource, SplitEvent, adjust_panel, split_adjust_panel


def _dates(n: int) -> list[str]:
    return [(date(2026, 1, 1) + timedelta(days=t)).isoformat() for t in range(n)]


def _panel(closes: list[float], sym: str = "SPL", vol: float = 1_000_000.0) -> dict:
    ds = _dates(len(closes))
    return {d: {sym: {"close": c, "dollar_vol": c * vol}} for d, c in zip(ds, closes, strict=True)}


def _closes(panel: dict, sym: str = "SPL") -> list[float]:
    return [panel[d][sym]["close"] for d in sorted(panel) if sym in panel[d]]


def _rising(n: int = 40, split_at: int = 21, factor: float = 0.1) -> list[float]:
    """A gently rising stock whose price is multiplied by `factor` from bar
    `split_at` on (10 = a 1:10 reverse split; 0.1 = a 10:1 split)."""
    return [round((20.0 + 0.1 * t) * (factor if t >= split_at else 1.0), 4) for t in range(n)]


def _event(exec_idx: int, split_from: float, split_to: float, sym: str = "SPL") -> SplitEvent:
    return SplitEvent(sym, _dates(60)[exec_idx], split_from, split_to)


# --- the issue's acceptance case: no fake ±90% trade -------------------------

_CASES = [
    pytest.param(10.0, 10, 1, id="reverse_1_for_10"),
    pytest.param(0.1, 1, 10, id="forward_10_for_1"),
]


@pytest.mark.parametrize(("factor", "sf", "st"), _CASES)
def test_split_event_back_adjusts_pre_split_closes(factor, sf, st):
    raw = _rising(factor=factor)
    out = split_adjust_panel(_panel(raw), [_event(21, sf, st)])
    adj = _closes(out)
    assert adj[21:] == raw[21:]  # post-split bars are the truth
    assert adj[20] == pytest.approx(raw[20] * factor)  # history restated in post-split units
    assert max(abs(b / a - 1) for a, b in zip(adj, adj[1:], strict=False)) < 0.01


@pytest.mark.parametrize(("factor", "sf", "st"), _CASES)
def test_backtest_on_adjusted_panel_books_no_split_trade(factor, sf, st):
    raw = _rising(factor=factor)
    kw = dict(entry_rsi=100, exit_rsi=101, ma_period=0, max_hold=5, min_price=0.0)
    ds = _dates(len(raw))
    raw_trades = mean_reversion_trades(ds, raw, [1e9] * len(raw), **kw)
    assert max(abs(t["ret"]) for t in raw_trades) > 0.85  # control: the bug books the split
    adj = _closes(split_adjust_panel(_panel(raw), [_event(21, sf, st)]))
    adj_trades = mean_reversion_trades(ds, adj, [1e9] * len(adj), **kw)
    assert max(abs(t["ret"]) for t in adj_trades) < 0.05


# --- the cases the rejected heuristic got wrong -------------------------------


def test_real_spike_without_a_split_event_is_left_alone():
    """CDTX 2025-11-14 +105%: a real move near a 2:1 ratio. No event -> untouched."""
    raw = [103.79, 108.19, 107.12, 105.99, 217.71, 217.91, 218.41]
    panel = _panel(raw, sym="CDTX")
    other = SplitEvent("OTHER", _dates(10)[4], 1, 2)  # an event for a different symbol
    assert split_adjust_panel(panel, [other]) == panel
    assert split_adjust_panel(panel, []) == panel


def test_split_with_a_large_same_day_move_is_adjusted_and_keeps_the_move():
    """MUU 2026-07: 1->20 split (execution 07-15); the cache's jump is on 07-14
    (fetched after Polygon knew), with a real +9.5% move on top (ratio 0.0548)."""
    raw = [703.05, 764.63, 745.39, 678.2, 37.1325, 31.27, 27.51]
    out = _closes(split_adjust_panel(_panel(raw, sym="MUU"), [_event(5, 1, 20, "MUU")]), "MUU")
    assert out[4:] == raw[4:]
    assert out[3] == pytest.approx(678.2 / 20)
    assert out[4] / out[3] - 1 == pytest.approx(37.1325 / 33.91 - 1)  # the real +9.5%
    assert min(b / a - 1 for a, b in zip(out, out[1:], strict=False)) > -0.2


def test_boundary_on_the_execution_date_itself():
    raw = _rising(factor=0.5)
    adj = _closes(split_adjust_panel(_panel(raw), [_event(21, 1, 2)]))
    assert max(abs(b / a - 1) for a, b in zip(adj, adj[1:], strict=False)) < 0.01


def test_already_adjusted_history_is_not_adjusted_twice():
    """KORU 2025-02-10 1:10 reverse split: the rows were backfilled after the
    split, so the cache shows no jump. Applying the event again would multiply
    the history by 10."""
    raw = [40.7, 42.3, 42.6, 40.1, 42.38, 42.38, 42.36, 45.11]
    panel = _panel(raw, sym="KORU")
    assert split_adjust_panel(panel, [_event(4, 10, 1, "KORU")]) == panel


def test_jump_far_from_the_execution_date_is_not_attributed_to_the_split():
    raw = _rising(factor=0.1, split_at=10)
    panel = _panel(raw)
    # event 11 sessions after the jump: out of the as-of window -> not applied
    assert split_adjust_panel(panel, [_event(21, 1, 10)]) == panel


def test_event_outside_the_cached_range_is_a_noop():
    panel = _panel(_rising(n=10, factor=1.0))
    late = SplitEvent("SPL", "2027-01-05", 1, 10)
    early = SplitEvent("SPL", "2020-01-05", 1, 10)
    assert split_adjust_panel(panel, [late, early]) == panel


def test_consecutive_splits_compound():
    raw = [100.0, 100.0, 50.0, 50.0, 5.0, 5.0]  # 2:1 then 10:1
    out = split_adjust_panel(_panel(raw), [_event(2, 1, 2), _event(4, 1, 10)])
    assert _closes(out) == pytest.approx([5.0] * 6)


def test_panel_is_not_mutated_and_dollar_volume_stays_raw():
    raw = _rising(factor=0.1)
    panel = _panel(raw)
    snapshot = json.loads(json.dumps(panel))
    out = split_adjust_panel(panel, [_event(21, 1, 10)])
    assert panel == snapshot
    ds = sorted(panel)
    assert [out[d]["SPL"]["dollar_vol"] for d in ds] == [panel[d]["SPL"]["dollar_vol"] for d in ds]


def test_symbol_missing_on_some_days_uses_its_own_sessions():
    raw = _rising(n=30, factor=0.1, split_at=15)
    panel = _panel(raw)
    ds = sorted(panel)
    for d in ds[:15]:
        panel[d]["AAA"] = {"close": 1.0, "dollar_vol": 1.0}
    out = split_adjust_panel(panel, [_event(15, 1, 10)])
    assert max(abs(b / a - 1) for a, b in zip(_closes(out), _closes(out)[1:], strict=False)) < 0.02
    assert _closes(out, "AAA") == [1.0] * 15


def test_adjust_panel_counts_only_located_events():
    raw = _rising(factor=0.1)
    evs = [_event(21, 1, 10), _event(5, 1, 3), SplitEvent("SPL", "2026-01-09", 1, 1)]
    _, applied = adjust_panel(_panel(raw), evs)
    assert applied == 1


def test_event_just_after_the_last_cached_session_can_adjust_it():
    """The cache's last row may already be as-of adjusted for a split executing
    on the next session (the window reaches back from the execution date)."""
    raw = _rising(n=22, factor=0.1)  # jump on the last cached bar (index 21)
    out = split_adjust_panel(_panel(raw), [_event(22, 1, 10)])
    assert max(abs(b / a - 1) for a, b in zip(_closes(out), _closes(out)[1:], strict=False)) < 0.01


def test_split_event_price_factor():
    assert SplitEvent("X", "2026-01-01", 1, 20).price_factor == pytest.approx(0.05)
    assert SplitEvent("X", "2026-01-01", 10, 1).price_factor == pytest.approx(10.0)


# --- Polygon source + cache ----------------------------------------------------


class _FakeHttp:
    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.urls: list[str] = []

    def __call__(self, url, timeout=20, **_):
        self.urls.append(url)
        return self.pages.pop(0)


def _rows(*items):
    return [
        {"ticker": t, "execution_date": d, "split_from": f, "split_to": to, "id": f"{t}{d}"}
        for t, d, f, to in items
    ]


def test_source_fetches_range_paginates_and_parses(monkeypatch, tmp_path):
    http = _FakeHttp(
        [
            {"results": _rows(("MUU", "2026-07-15", 1, 20)), "next_url": "https://x/next?cursor=c"},
            {"results": _rows(("LABX", "2026-07-21", 1, 6))},
        ]
    )
    monkeypatch.setattr(splits_mod, "get_json", http)
    src = PolygonSplitSource("KEY", cache_path=str(tmp_path / "s.db"), today=date(2026, 9, 29))
    got = src.fetch_range("2026-07-01", "2026-07-31")
    assert got == [
        SplitEvent("MUU", "2026-07-15", 1.0, 20.0),
        SplitEvent("LABX", "2026-07-21", 1.0, 6.0),
    ]
    assert "execution_date.gte=2026-07-01" in http.urls[0]
    assert "reference/splits" in http.urls[0]
    assert http.urls[1] == "https://x/next?cursor=c&apiKey=KEY"


def test_source_caches_a_past_range_and_serves_it_offline(monkeypatch, tmp_path):
    http = _FakeHttp([{"results": _rows(("MUU", "2026-07-15", 1, 20))}])
    monkeypatch.setattr(splits_mod, "get_json", http)
    path = str(tmp_path / "s.db")
    PolygonSplitSource("KEY", cache_path=path, today=date(2026, 9, 29)).fetch_range(
        "2026-07-01", "2026-07-31"
    )
    # new instance, no HTTP left: a covered sub-range comes from disk
    again = PolygonSplitSource("KEY", cache_path=path, today=date(2026, 9, 30))
    assert again.fetch_range("2026-07-10", "2026-07-20") == [
        SplitEvent("MUU", "2026-07-15", 1.0, 20.0)
    ]
    assert again.fetch_range("2026-07-16", "2026-07-20") == []
    assert len(http.urls) == 1


def test_source_refetches_a_range_that_was_not_in_the_past_when_fetched(monkeypatch, tmp_path):
    """Splits are announced ahead; a range fetched while still open may be incomplete."""
    http = _FakeHttp([{"results": []}, {"results": _rows(("NEW", "2026-09-28", 1, 2))}])
    monkeypatch.setattr(splits_mod, "get_json", http)
    path = str(tmp_path / "s.db")
    PolygonSplitSource("KEY", cache_path=path, today=date(2026, 9, 20)).fetch_range(
        "2026-09-01", "2026-09-30"
    )
    later = PolygonSplitSource("KEY", cache_path=path, today=date(2026, 10, 5))
    assert later.fetch_range("2026-09-01", "2026-09-30") == [
        SplitEvent("NEW", "2026-09-28", 1.0, 2.0)
    ]
    assert len(http.urls) == 2


def test_source_reuses_an_open_range_fetched_the_same_day(monkeypatch, tmp_path):
    """Repeated backtests on a recent window must not re-page the API every run;
    an open range is refreshed at most once a day."""
    http = _FakeHttp([{"results": _rows(("NEW", "2026-09-28", 1, 2))}])
    monkeypatch.setattr(splits_mod, "get_json", http)
    path = str(tmp_path / "s.db")
    for _ in range(3):
        src = PolygonSplitSource("KEY", cache_path=path, today=date(2026, 9, 29))
        assert src.fetch_range("2026-09-01", "2026-09-25") == [
            SplitEvent("NEW", "2026-09-28", 1.0, 2.0)
        ]
    assert len(http.urls) == 1


def test_source_skips_malformed_rows(monkeypatch, tmp_path):
    rows = _rows(("OK", "2026-07-15", 1, 2)) + [
        {"ticker": "BAD", "execution_date": "2026-07-15", "split_from": 0, "split_to": 2},
        {"ticker": "", "execution_date": "2026-07-15", "split_from": 1, "split_to": 2},
    ]
    monkeypatch.setattr(splits_mod, "get_json", _FakeHttp([{"results": rows}]))
    src = PolygonSplitSource("KEY", cache_path="", today=date(2026, 9, 29))
    assert src.fetch_range("2026-07-01", "2026-07-31") == [SplitEvent("OK", "2026-07-15", 1, 2)]


def test_source_http_error_propagates(monkeypatch):
    """A failed fetch must not degrade to 'no splits' (that is the raw-closes bug)."""

    def boom(url, timeout=20, **_):
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

    monkeypatch.setattr(splits_mod, "get_json", boom)
    with pytest.raises(urllib.error.HTTPError):
        PolygonSplitSource("KEY").fetch_range("2026-07-01", "2026-07-31")


def test_source_requires_api_key():
    with pytest.raises(RuntimeError):
        PolygonSplitSource("")
