"""AlpacaTradingClient.get_shortability — thin wrapper over get_asset.

Confirmed live that our target runners are not shortable (APIError 42210000); the
accumulation job records this per setup, so the wrapper must surface the
tradable/shortable/easy_to_borrow flags without leaking the SDK type.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.alpaca_client import AlpacaTradingClient


def _client_with_asset(asset) -> AlpacaTradingClient:
    c = AlpacaTradingClient.__new__(AlpacaTradingClient)
    inner = MagicMock()
    inner.get_asset.return_value = asset
    c._client = inner
    return c


def test_get_shortability_maps_asset_flags():
    asset = SimpleNamespace(tradable=True, shortable=False, easy_to_borrow=False)
    c = _client_with_asset(asset)
    status = c.get_shortability("ILLR")
    assert status == {"tradable": True, "shortable": False, "easy_to_borrow": False}
    c._client.get_asset.assert_called_once_with("ILLR")


def test_get_shortability_true_for_liquid_name():
    asset = SimpleNamespace(tradable=True, shortable=True, easy_to_borrow=True)
    status = _client_with_asset(asset).get_shortability("AAPL")
    assert status["shortable"] is True and status["easy_to_borrow"] is True


def test_get_shortability_defaults_false_on_missing_flags():
    asset = SimpleNamespace(tradable=True)  # older/edge assets may omit fields
    status = _client_with_asset(asset).get_shortability("XYZ")
    assert status["shortable"] is False and status["easy_to_borrow"] is False
