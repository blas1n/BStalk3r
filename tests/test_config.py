"""Config: paper-trading safety guard + param builders."""

from __future__ import annotations

import pytest
from src.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_paper_settings_pass_safety():
    _settings().validate_paper_safety()  # must not raise


def test_paper_false_is_rejected():
    with pytest.raises(RuntimeError):
        _settings(paper=False).validate_paper_safety()


def test_live_base_url_is_rejected():
    with pytest.raises(RuntimeError):
        _settings(alpaca_base_url="https://api.alpaca.markets").validate_paper_safety()


def test_universe_symbols_parsed_and_uppercased():
    s = _settings(universe="aapl, tsla ,nvda")
    assert s.universe_symbols() == ["AAPL", "TSLA", "NVDA"]


def test_builders_round_trip_values():
    s = _settings(min_rvol=9.0, stop_loss_pct=0.04, max_concurrent_positions=3)
    assert s.build_scan_filters().min_rvol == 9.0
    assert s.build_entry_params().min_rvol == 9.0
    assert s.build_exit_params().stop_loss_pct == 0.04
    assert s.build_risk_params().max_concurrent_positions == 3
    assert s.build_exec_params().limit_slippage_pct == s.limit_slippage_pct
