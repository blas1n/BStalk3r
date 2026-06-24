"""Minute-bar normalization: RTH filter + field mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from src.minute_bars import polygon_minutes_to_bars


def _ms(h, m):  # UTC epoch ms for a 2026-06-09 HH:MM bar
    return int(datetime(2026, 6, 9, h, m, tzinfo=UTC).timestamp() * 1000)


def test_filters_to_regular_hours_et():
    payload = {
        "results": [
            {"t": _ms(12, 0), "o": 1, "h": 1, "l": 1, "c": 1, "v": 5},  # 08:00 ET pre-market
            {"t": _ms(13, 30), "o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 9},  # 09:30 ET open
            {"t": _ms(19, 59), "o": 4, "h": 4, "l": 4, "c": 4, "v": 7},  # 15:59 ET
            {"t": _ms(20, 30), "o": 9, "h": 9, "l": 9, "c": 9, "v": 1},  # 16:30 ET after-hours
        ]
    }
    bars = polygon_minutes_to_bars(payload)
    assert [b["close"] for b in bars] == [2.5, 4.0]  # only the two RTH bars
    assert bars[0]["high"] == 3.0 and bars[0]["low"] == 1.5


def test_sorted_and_handles_missing_fields():
    payload = {
        "results": [
            {"t": _ms(14, 0), "c": 5.0},  # ohl missing -> default to close
            {"t": _ms(13, 45), "o": 4, "h": 4.2, "l": 3.9, "c": 4.1},
            {"t": _ms(15, 0), "h": None, "c": None},  # incomplete -> skipped
        ]
    }
    bars = polygon_minutes_to_bars(payload)
    assert [b["close"] for b in bars] == [4.1, 5.0]  # sorted by ts, incomplete dropped
    assert bars[1]["high"] == 5.0  # defaulted from close


def test_empty_payload():
    assert polygon_minutes_to_bars({"status": "OK"}) == []
