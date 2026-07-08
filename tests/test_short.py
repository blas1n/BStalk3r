"""Short (fade) simulation: enter short at the +X% cross, inverted exits.

Stop = adverse UP move (the squeeze risk, triggered on the bar HIGH and filled at
the stop level); take-profit = favorable DOWN move (on the bar LOW); trailing
tracks the trough. `max_adverse_pct` records the worst up-excursion so the squeeze
tail is visible even when the stop caps the assumed loss.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.intraday import simulate_short_trade
from src.strategy import ExitParams

T0 = datetime(2026, 7, 2, 13, 30, tzinfo=UTC)

EXIT = ExitParams(
    stop_loss_pct=0.05,  # short stops if price rises 5%
    take_profit_pct=0.10,  # short covers if price falls 10%
    scale_out_fraction=0.5,
    trailing_stop_pct=0.15,
    max_hold_minutes=180,
    exit_spread_pct=1.5,
)
PREV = 10.0  # entry trigger +5% -> short at first close >= 10.5


def _bars(rows):
    # rows: (high, low, close)
    out = []
    for i, (h, low, c) in enumerate(rows):
        out.append({"ts": T0 + timedelta(minutes=i), "open": c, "high": h, "low": low, "close": c})
    return out


def _t(rows, exit_params=EXIT, cost_fn=None):
    return simulate_short_trade(_bars(rows), PREV, 5.0, 1.0, 50.0, exit_params, cost_fn=cost_fn)


def test_fade_pays_off_take_profit():
    # enter short ~10.6, price falls to 9.5 (-10% from entry) -> favorable cover
    t = _t([(10.6, 10.6, 10.6), (10.0, 9.5, 9.6), (9.5, 9.4, 9.5)])
    assert t.entered and t.exit_reason == "take_profit"
    # short profit = (entry - tp_level)/entry ; tp_level = 10.6*0.9
    assert t.net_return_pct > 0


def test_squeeze_stops_the_short():
    # enter ~10.6, next bar spikes to 11.2 (+5.7% adverse) -> stop
    t = _t([(10.6, 10.6, 10.6), (11.2, 10.6, 11.0), (11.0, 11.0, 11.0)])
    assert t.exit_reason == "stop_loss"
    assert t.net_return_pct < 0  # short lost on the up-move


def test_max_adverse_captures_squeeze_magnitude():
    # a violent squeeze: bar high +40% even though we assume stop fill at +5%
    t = _t([(10.6, 10.6, 10.6), (14.0, 10.6, 13.0)])
    assert t.exit_reason == "stop_loss"
    # max_adverse reflects the true intrabar spike (~+32% from entry 10.6), not +5%
    assert t.max_adverse_pct > 25  # the real slippage/squeeze risk is visible


def test_session_end_covers_flat():
    t = _t([(10.6, 10.6, 10.6), (10.6, 10.6, 10.6)])  # nothing triggers
    assert t.exit_reason in ("force_close", "session_end")


def test_no_entry_when_no_cross():
    t = _t([(10.1, 10.0, 10.1), (10.2, 10.1, 10.2)])  # never +5%
    assert t.entered is False


def test_cost_reduces_short_net():
    gross = _t([(10.6, 10.6, 10.6), (10.0, 9.5, 9.6), (9.5, 9.4, 9.5)])
    net = _t([(10.6, 10.6, 10.6), (10.0, 9.5, 9.6), (9.5, 9.4, 9.5)], cost_fn=lambda p: 4.0)
    assert abs(net.net_return_pct - (gross.gross_return_pct - 4.0)) < 1e-9
