"""Execution: conservative limit pricing + dry-run vs live order paths.

No real Alpaca calls — a fake client records what would have been sent.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.database import Database
from src.execution import ExecParams, ExecutionEngine, conservative_buy_limit
from src.models import OrderSide

from tests.conftest import make_snapshot

PARAMS = ExecParams(limit_slippage_pct=0.003, order_fill_timeout_sec=20)


# ---- pure pricing ---------------------------------------------------------


def test_limit_uses_ask_when_tighter_than_slippage_cap():
    # ask 8.52 < last*1.003 = 8.5256 -> use ask
    assert conservative_buy_limit(ask=8.52, last=8.50, slippage_pct=0.003) == 8.52


def test_limit_uses_slippage_cap_when_ask_is_wide():
    # ask 9.00 > last*1.003=8.53 -> cap at slippage bound
    assert conservative_buy_limit(ask=9.00, last=8.50, slippage_pct=0.003) == 8.53


def test_limit_falls_back_to_last_when_no_ask():
    assert conservative_buy_limit(ask=None, last=8.50, slippage_pct=0.003) == 8.53


# ---- fake client ----------------------------------------------------------


@dataclass
class _FakeOrder:
    id: str
    status: str


class _FakeClient:
    def __init__(self):
        self.calls = []

    def submit_limit_order(self, symbol, qty, side, limit_price):
        self.calls.append((symbol, qty, side, limit_price))
        return _FakeOrder(id="ord-1", status="accepted")


def _engine(tmp_path, dry_run):
    db = Database(str(tmp_path / "e.db"))
    db.init_schema()
    return ExecutionEngine(_FakeClient(), db, PARAMS, dry_run=dry_run), db


def test_dry_run_places_no_order_but_logs(tmp_path):
    engine, db = _engine(tmp_path, dry_run=True)
    result = engine.submit_entry(
        make_snapshot(symbol="RUNR", last_price=8.50, ask_price=8.52), qty=100
    )

    assert result["status"] == "dry_run"
    assert engine.client.calls == []  # never touched the broker
    order = db.get_order(result["order_id"])
    assert order["status"] == "dry_run"
    assert order["side"] == "buy"
    assert order["limit_price"] == 8.52


def test_live_path_submits_and_records_alpaca_id(tmp_path):
    engine, db = _engine(tmp_path, dry_run=False)
    result = engine.submit_entry(
        make_snapshot(symbol="RUNR", last_price=8.50, ask_price=8.52), qty=100
    )

    assert len(engine.client.calls) == 1
    symbol, qty, side, limit_price = engine.client.calls[0]
    assert (symbol, qty, side) == ("RUNR", 100, OrderSide.BUY)
    assert result["alpaca_order_id"] == "ord-1"
    assert db.get_order(result["order_id"])["alpaca_order_id"] == "ord-1"


def test_exit_uses_sell_side(tmp_path):
    engine, db = _engine(tmp_path, dry_run=False)
    result = engine.submit_exit(symbol="RUNR", qty=50, last_price=11.5, reason="take_profit_scale")
    _, qty, side, _ = engine.client.calls[0]
    assert side == OrderSide.SELL
    assert qty == 50
    assert db.get_order(result["order_id"])["reason"] == "take_profit_scale"


def test_broker_failure_is_recorded_not_raised(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    db.init_schema()

    class _Boom:
        def submit_limit_order(self, *a, **k):
            raise RuntimeError("api down")

    engine = ExecutionEngine(_Boom(), db, PARAMS, dry_run=False)
    result = engine.submit_entry(make_snapshot(), qty=100)
    assert result["status"] == "error"
    assert db.get_order(result["order_id"])["status"] == "error"
