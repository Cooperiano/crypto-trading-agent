"""Execution engine — order lifecycle management (paper and live modes)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog

from trading_agent.config import TradingConfig
from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Side, Signal, Trade
from trading_agent.security import SecurityManager

logger = structlog.get_logger(__name__)


class PaperTradingSimulator:
    def __init__(self, market_data: Any = None) -> None:
        self._market_data = market_data
        self._trades: list[Trade] = []
        self._balance: float = 300.0

    async def execute(self, signal: Signal) -> Trade:
        book = self._market_data.get_book(signal.symbol) if self._market_data else None
        fill_price = (book.best_ask if signal.side == Side.BUY else book.best_bid) if book and (book.best_ask if signal.side == Side.BUY else book.best_bid) else signal.price
        cost = fill_price * signal.size
        if signal.side == Side.BUY and cost > self._balance:
            signal = signal.model_copy(update={"size": self._balance / fill_price})
            cost = fill_price * signal.size
        trade = Trade(order_id=f"paper-{uuid.uuid4().hex[:8]}", symbol=signal.symbol, side=signal.side,
                      price=fill_price, size=signal.size, fee=cost * 0.001, fee_currency="USDT",
                      timestamp=time.time(), strategy=signal.strategy)
        if signal.side == Side.BUY:
            self._balance -= cost
        else:
            self._balance += fill_price * signal.size
        self._trades.append(trade)
        logger.info("paper_fill", order_id=trade.order_id, side=trade.side.value, price=trade.price, size=trade.size, balance=f"${self._balance:.2f}")
        return trade

    def get_balance(self) -> float:
        return self._balance

    def get_trades(self) -> list[Trade]:
        return list(self._trades)


class ExecutionEngine:
    def __init__(self, config: TradingConfig, event_bus: EventBus, binance_client: Any | None,
                 market_data: Any, security_manager: SecurityManager | None = None) -> None:
        self._config = config
        self._bus = event_bus
        self._binance = binance_client
        self._market_data = market_data
        self._security = security_manager
        self._paper = PaperTradingSimulator(market_data) if config.mode == "paper" else None
        self._bus.subscribe(EventType.ORDER_SUBMITTED, self._on_order)

    async def _on_order(self, event: Event) -> None:
        signal: Signal = event.data["signal"]
        try:
            if self._security:
                signal = self._security.check_signal(signal, self._paper.get_balance() if self._paper else 0)
            trade = await (self._paper.execute(signal) if self._config.mode == "paper" else self._execute_live(signal))
            await self._bus.publish(Event(type=EventType.ORDER_FILLED, data={"trade": trade}, source="execution_engine"))
            if self._security:
                pnl = float(trade.size) * (float(trade.price) if trade.side == Side.SELL else -float(trade.price))
                self._security.record_trade_result(pnl, {"order_id": trade.order_id, "symbol": trade.symbol,
                    "side": trade.side.value, "price": trade.price, "size": trade.size})
        except Exception as e:
            logger.error("execution_error", error=str(e))
            await self._bus.publish(Event(type=EventType.ORDER_REJECTED, data={"signal": signal, "error": str(e)}, source="execution_engine"))

    async def _execute_live(self, signal: Signal) -> Trade:
        method = self._binance.place_market_order if signal.order_type.value == "MARKET" else self._binance.place_limit_order
        args = {"symbol": signal.symbol, "side": signal.side.value, "amount": signal.size}
        if signal.order_type.value != "MARKET":
            args["price"] = signal.price
        resp = await method(**args)
        return Trade(order_id=str(resp.get("id", "")), symbol=signal.symbol, side=signal.side,
                     price=float(resp.get("average", signal.price) or signal.price),
                     size=float(resp.get("filled", signal.size) or signal.size),
                     fee=float((resp.get("fee", {}) or {}).get("cost", 0) or 0),
                     fee_currency=str((resp.get("fee", {}) or {}).get("currency", "USDT") or "USDT"),
                     timestamp=resp.get("timestamp", time.time()) or time.time(), strategy=signal.strategy)
