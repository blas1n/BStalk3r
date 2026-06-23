"""Shared Polygon HTTP helper: 429-aware retry/backoff."""

from __future__ import annotations

import email.message
import urllib.error

import pytest
import src.polygon_http as ph


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", code, "err", hdrs, None)


def _patch(monkeypatch, sequence):
    """urlopen yields each item in `sequence`; raise it if it's an exception."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(url, timeout=None):
        item = sequence[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ph.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ph.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_success_first_try_no_sleep(monkeypatch):
    _, sleeps = _patch(monkeypatch, [_FakeResp(b'{"ok": 1}')])
    assert ph.get_json("http://x") == {"ok": 1}
    assert sleeps == []


def test_retries_on_429_then_succeeds(monkeypatch):
    calls, sleeps = _patch(
        monkeypatch, [_http_error(429), _http_error(429), _FakeResp(b'{"ok": 2}')]
    )
    assert ph.get_json("http://x", base_sleep=1.0) == {"ok": 2}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept before each retry


def test_429_honors_retry_after_header(monkeypatch):
    _, sleeps = _patch(monkeypatch, [_http_error(429, retry_after="7"), _FakeResp(b"{}")])
    ph.get_json("http://x", base_sleep=99.0)
    assert sleeps == [7.0]  # used header, not base_sleep


def test_429_exhausts_retries_then_raises(monkeypatch):
    _patch(monkeypatch, [_http_error(429), _http_error(429)])
    with pytest.raises(urllib.error.HTTPError) as ei:
        ph.get_json("http://x", retries=2, base_sleep=0.0)
    assert ei.value.code == 429


def test_non_429_raises_immediately(monkeypatch):
    calls, sleeps = _patch(monkeypatch, [_http_error(403), _FakeResp(b"{}")])
    with pytest.raises(urllib.error.HTTPError) as ei:
        ph.get_json("http://x")
    assert ei.value.code == 403
    assert calls["n"] == 1  # no retry on 403
    assert sleeps == []
