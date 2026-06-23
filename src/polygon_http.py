"""Shared Polygon HTTP fetch with 429-aware retry.

Polygon's free tier is 5 req/min; bursts (the grouped scan + the per-runner
outcome tracker) periodically hit `429 Too Many Requests`. Left unhandled it
crashes the daily job. This wrapper backs off (honoring `Retry-After`) and
retries on 429; every other HTTPError is re-raised so callers keep their own
403/404 ("no data") handling.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


def get_json(
    url: str, timeout: int = 20, retries: int = 4, base_sleep: float = 15.0
) -> dict[str, Any]:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed https host
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(_retry_after(exc, base_sleep))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _retry_after(exc: urllib.error.HTTPError, base_sleep: float) -> float:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw and str(raw).strip().isdigit():
        return float(raw)
    return base_sleep
