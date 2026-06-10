"""Order placement — paper only, limit-only, dry-run aware.

The engine never decides *whether* to trade (strategy + risk do that); it only
turns an approved decision into a logged broker action. Every attempt — including
dry-run and failures — is written to the orders table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.database import Database
from src.models import OrderSide


@dataclass(frozen=True)
class ExecParams:
    limit_slippage_pct: float
    order_fill_timeout_sec: int


class BrokerClient(Protocol):
    """Minimal surface the engine needs — implemented by AlpacaTradingClient."""

    def submit_limit_order(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> Any: ...


def conservative_buy_limit(ask: float | None, last: float, slippage_pct: float) -> float:
    """Buy limit = min(ask, last * (1 + slippage)) — the more conservative price.

    Never chase: cap how far above the last trade we are willing to pay, and
    if the ask is tighter than that cap, use the ask.
    """
    cap = round(last * (1 + slippage_pct), 2)
    if ask is None or ask <= 0:
        return cap
    return round(min(ask, cap), 2)


class ExecutionEngine:
    def __init__(
        self,
        client: BrokerClient,
        db: Database,
        params: ExecParams,
        dry_run: bool,
        logger: Any | None = None,
    ):
        self.client = client
        self.db = db
        self.params = params
        self.dry_run = dry_run
        self.log = logger

    def _log(self, event: str, **kw: Any) -> None:
        if self.log is not None:
            self.log.info(event, **kw)

    def submit_entry(self, snapshot: Any, qty: int, signal_id: int | None = None) -> dict[str, Any]:
        """Place (or simulate) a limit BUY for an approved candidate."""
        limit_price = conservative_buy_limit(
            getattr(snapshot, "ask_price", None),
            snapshot.last_price,
            self.params.limit_slippage_pct,
        )
        reason = f"entry signal_id={signal_id}" if signal_id else "entry"
        return self._place(snapshot.symbol, OrderSide.BUY, qty, limit_price, reason)

    def submit_exit(self, symbol: str, qty: int, last_price: float, reason: str) -> dict[str, Any]:
        """Place (or simulate) a limit SELL to reduce/close a position.

        Sell limit sits slightly *below* last to favour a fill on the way out.
        """
        limit_price = round(last_price * (1 - self.params.limit_slippage_pct), 2)
        return self._place(symbol, OrderSide.SELL, qty, limit_price, reason)

    def _place(
        self, symbol: str, side: OrderSide, qty: int, limit_price: float, reason: str
    ) -> dict[str, Any]:
        if self.dry_run:
            order_id = self.db.insert_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="limit",
                limit_price=limit_price,
                status="dry_run",
                reason=reason,
            )
            self._log("dry_run_order", symbol=symbol, side=side.value, qty=qty, limit=limit_price)
            return {"status": "dry_run", "order_id": order_id, "limit_price": limit_price}

        try:
            broker_order = self.client.submit_limit_order(symbol, qty, side, limit_price)
        except Exception as exc:  # noqa: BLE001 — broker failure must never crash the loop
            order_id = self.db.insert_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="limit",
                limit_price=limit_price,
                status="error",
                reason=f"{reason} | {exc}",
            )
            self._log("order_error", symbol=symbol, error=str(exc))
            return {"status": "error", "order_id": order_id, "limit_price": limit_price}

        alpaca_id = str(getattr(broker_order, "id", ""))
        status = str(getattr(broker_order, "status", "submitted"))
        order_id = self.db.insert_order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type="limit",
            limit_price=limit_price,
            status=status,
            reason=reason,
            alpaca_order_id=alpaca_id,
        )
        self._log("order_submitted", symbol=symbol, side=side.value, qty=qty, alpaca_id=alpaca_id)
        return {
            "status": status,
            "order_id": order_id,
            "alpaca_order_id": alpaca_id,
            "limit_price": limit_price,
        }
