"""Domain models shared across all modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class Candle(BaseModel):
    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class Ticker(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    timestamp: float = 0.0


class OrderBookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    symbol: str
    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []
    timestamp: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float | None:
        if self.spread is not None and self.mid is not None and self.mid > 0:
            return self.spread / self.mid
        return None


class Signal(BaseModel):
    strategy: str
    symbol: str
    side: Side
    price: float
    size: float
    confidence: float
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    stop_price: float | None = None
    reason: str = ""
    metadata: dict[str, Any] = {}


class Position(BaseModel):
    symbol: str
    side: Side
    size: float
    avg_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    max_price: float = 0.0
    min_price: float = float("inf")

    def update_pnl(self, current_price: float) -> None:
        self.current_price = current_price
        self.max_price = max(self.max_price, self.avg_price, current_price)
        self.min_price = min(self.min_price if self.min_price != float("inf") else current_price, current_price)
        if self.side == Side.BUY:
            self.unrealized_pnl = (current_price - self.avg_price) * self.size
        else:
            self.unrealized_pnl = (self.avg_price - current_price) * self.size


class Trade(BaseModel):
    order_id: str
    symbol: str
    side: Side
    price: float
    size: float
    fee: float
    fee_currency: str = "USDT"
    timestamp: float
    strategy: str
