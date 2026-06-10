"""Shared fixtures for BStalk3r unit tests."""

from __future__ import annotations

from src.models import MarketSnapshot


def make_snapshot(**overrides) -> MarketSnapshot:
    """A snapshot that passes every default entry filter unless overridden."""
    defaults = dict(
        symbol="RUNR",
        last_price=8.50,
        day_change_pct=18.0,
        rvol=12.0,
        volume_acceleration=5.0,
        spread_pct=0.4,
        bid_price=8.48,
        ask_price=8.52,
        float_shares=None,
        market_cap=None,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)
