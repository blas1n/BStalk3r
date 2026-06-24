"""Intraday hit-and-run simulation. Pure — reuses strategy.evaluate_exit over
minute bars, so the backtest can't drift from the live exit rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.intraday import IntradayTrade, aggregate, reconstruct_entry, simulate_trade
from src.strategy import ExitParams

T0 = datetime(2026, 6, 9, 13, 30, tzinfo=UTC)  # 09:30 ET

EXIT = ExitParams(
    stop_loss_pct=0.05,
    take_profit_pct=0.15,
    scale_out_fraction=0.5,
    trailing_stop_pct=0.08,
    max_hold_minutes=30,
    exit_spread_pct=1.5,
)


def _bars(closes, highs=None):
    """Minute bars from close prices (high defaults to close)."""
    highs = highs or closes
    out = []
    for i, (c, h) in enumerate(zip(closes, highs, strict=True)):
        out.append(
            {"ts": T0 + timedelta(minutes=i), "open": c, "high": h, "low": min(c, h), "close": c}
        )
    return out


PREV_CLOSE = 10.0  # entry triggers at +5% -> close >= 10.5


# ---- entry reconstruction ----


def test_entry_at_first_threshold_cross():
    bars = _bars([10.0, 10.2, 10.6, 11.0])  # crosses +5% at idx 2 (10.6)
    idx = reconstruct_entry(bars, PREV_CLOSE, entry_min_change=5.0, min_price=1.0, max_price=50.0)
    assert idx == 2


def test_no_entry_if_never_crosses():
    bars = _bars([10.0, 10.1, 10.2])  # never +5%
    assert reconstruct_entry(bars, PREV_CLOSE, 5.0, 1.0, 50.0) is None


def test_entry_respects_price_band():
    bars = _bars([60.0, 61.0])  # +500% but above max_price 50
    assert reconstruct_entry(bars, PREV_CLOSE, 5.0, 1.0, 50.0) is None


# ---- exit simulation (reusing evaluate_exit) ----


def _trade(closes, highs=None, exit_params=EXIT, cost_fn=None):
    return simulate_trade(
        _bars(closes, highs), PREV_CLOSE, 5.0, 1.0, 50.0, exit_params, cost_fn=cost_fn
    )


def test_stop_loss_exit():
    # trailing bar so the stop (not the session-end force) is what fires
    t = _trade([10.6, 10.0, 10.0])  # -5.7% from entry 10.6 at idx 1
    assert t.entered and t.exit_reason == "stop_loss"
    assert t.exit_price == 10.0


def test_take_profit_exit():
    t = _trade([10.6, 11.0, 12.3, 12.3])  # +16% at idx 2
    assert t.exit_reason == "take_profit_scale"
    assert t.entry_price == 10.6 and t.exit_price == 12.3


def test_trailing_stop_exit():
    # peaks at high 13.0, then closes drop > 8% off peak (fires at idx 2)
    t = _trade([10.6, 12.0, 11.5, 11.5], highs=[10.6, 13.0, 11.5, 11.5])
    assert t.exit_reason == "trailing_stop"


def test_max_hold_exit():
    short = ExitParams(0.05, 0.15, 0.5, 0.08, max_hold_minutes=3, exit_spread_pct=1.5)
    t = _trade([10.6, 10.6, 10.6, 10.6, 10.6], exit_params=short)  # flat, exits at 3 min
    assert t.exit_reason == "max_hold"
    assert t.held_min == 3


def test_session_end_force_close():
    t = _trade([10.6, 10.7])  # nothing triggers; last bar forces out
    assert t.exit_reason == "force_close"
    assert t.exit_price == 10.7


def test_net_return_applies_cost():
    gross = _trade([10.6, 12.3, 12.3])
    net = _trade([10.6, 12.3, 12.3], cost_fn=lambda price: 4.0)
    assert abs(net.gross_return_pct - gross.gross_return_pct) < 1e-9
    assert abs(net.net_return_pct - (gross.gross_return_pct - 4.0)) < 1e-9


def test_not_entered_trade_is_flagged():
    t = _trade([10.0, 10.1])  # never crosses entry
    assert t.entered is False


# ---- aggregate ----


def _tr(net, reason="x", held=10.0):
    return IntradayTrade(entered=True, net_return_pct=net, exit_reason=reason, held_min=held)


def test_aggregate_empty_is_none():
    assert aggregate([]) is None


def test_aggregate_metrics():
    a = aggregate([_tr(10.0), _tr(6.0), _tr(-4.0)])
    assert a["n"] == 3
    assert abs(a["avg"] - 4.0) < 1e-9
    assert a["median"] == 6.0
    assert abs(a["win_rate"] - (2 / 3) * 100) < 1e-9


def test_aggregate_counts_reasons():
    a = aggregate([_tr(1, "stop_loss"), _tr(2, "stop_loss"), _tr(3, "max_hold")])
    assert a["reasons"]["stop_loss"] == 2
    assert a["reasons"]["max_hold"] == 1
