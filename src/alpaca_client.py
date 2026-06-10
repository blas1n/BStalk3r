"""Thin wrapper over alpaca-py's TradingClient (paper trading only).

Isolates every Alpaca SDK type behind a small surface so the rest of the code
depends on our own models, not the broker SDK. Construction asserts paper mode.
"""

from __future__ import annotations

from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from src.models import OrderSide

_SIDE_MAP = {OrderSide.BUY: AlpacaOrderSide.BUY, OrderSide.SELL: AlpacaOrderSide.SELL}


class AlpacaTradingClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        if not paper:
            raise RuntimeError("AlpacaTradingClient is paper-only; refusing paper=False.")
        self._client = TradingClient(api_key, secret_key, paper=True)

    def get_account(self) -> Any:
        return self._client.get_account()

    def get_open_positions(self) -> list[Any]:
        return self._client.get_all_positions()

    def submit_limit_order(self, symbol: str, qty: int, side: OrderSide, limit_price: float) -> Any:
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_SIDE_MAP[side],
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
        return self._client.submit_order(order_data=request)

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)

    def get_order(self, order_id: str) -> Any:
        return self._client.get_order_by_id(order_id)

    def close_position(self, symbol: str) -> Any:
        return self._client.close_position(symbol)
