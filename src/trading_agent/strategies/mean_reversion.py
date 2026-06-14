"""Mean-reversion strategy — Bollinger Bands based bounce trading."""

from __future__ import annotations

import structlog
from collections import deque

from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Side, Signal, OrderType
from trading_agent.strategies.base import BaseStrategy

logger = structlog.get_logger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """Bollinger Bands mean-reversion — buy at lower band, sell at upper band.

    Config params:
        bb_period: int = 20 — Bollinger Bands period
        bb_std: float = 2.0 — standard deviation multiplier
        rsi_period: int = 14 — RSI period
        rsi_oversold: float = 30 — oversold threshold
        rsi_overbought: float = 70 — overbought threshold
        max_position_usdt: float = 200.0
        cooldown_ticks: int = 15
    """

    def __init__(self, name: str, config: dict, event_bus: EventBus) -> None:
        super().__init__(name, config, event_bus)
        self._bb_period: int = config.get("bb_period", 20)
        self._bb_std: float = config.get("bb_std", 2.0)
        self._rsi_period: int = config.get("rsi_period", 14)
        self._rsi_oversold: float = config.get("rsi_oversold", 30.0)
        self._rsi_overbought: float = config.get("rsi_overbought", 70.0)
        self._max_position_usdt: float = config.get("max_position_usdt", 200.0)
        self._cooldown_ticks: int = config.get("cooldown_ticks", 15)
        self._prices: dict[str, deque[float]] = {}
        self._last_signal_tick: dict[str, int] = {}
        self._tick_count: int = 0

    async def start(self) -> None:
        self._bus.subscribe(EventType.ORDERBOOK_UPDATE, self.on_event)
        self._bus.subscribe(EventType.TICKER_UPDATE, self.on_event)
        logger.info("mean_reversion_started", bb_period=self._bb_period, bb_std=self._bb_std)

    async def stop(self) -> None:
        self._enabled = False

    async def on_event(self, event: Event) -> None:
        if not self._enabled:
            return

        price = event.data.get("mid") or event.data.get("last")
        symbol = event.data.get("symbol", "")
        if not price or not symbol or price <= 0:
            return

        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=max(self._bb_period * 2, 100))
        self._prices[symbol].append(price)

        self._tick_count += 1
        last_tick = self._last_signal_tick.get(symbol, -self._cooldown_ticks)
        if self._tick_count - last_tick < self._cooldown_ticks:
            return

        prices = self._prices[symbol]
        if len(prices) < self._bb_period:
            return

        prices_list = list(prices)
        close_prices = prices_list[-self._bb_period:]

        mean = sum(close_prices) / len(close_prices)
        variance = sum((p - mean) ** 2 for p in close_prices) / len(close_prices)
        std = variance ** 0.5

        upper = mean + self._bb_std * std
        lower = mean - self._bb_std * std
        rsi = self._calc_rsi(prices_list, self._rsi_period)

        if rsi is None:
            return

        if price <= lower and rsi <= self._rsi_oversold:
            size = self._max_position_usdt / price
            confidence = min((self._rsi_oversold - rsi) / self._rsi_oversold + 0.3, 0.9)
            await self.emit_signal(Signal(
                strategy=self.name, symbol=symbol, side=Side.BUY, price=price,
                size=round(size, 6), confidence=round(confidence, 2), order_type=OrderType.LIMIT,
                reason=f"BB oversold: price={price:.4f} below lower={lower:.4f}, RSI={rsi:.1f}",
                metadata={"bb_upper": upper, "bb_lower": lower, "bb_mid": mean, "rsi": rsi},
            ))
            self._last_signal_tick[symbol] = self._tick_count

        elif price >= upper and rsi >= self._rsi_overbought:
            size = self._max_position_usdt / price
            confidence = min((rsi - self._rsi_overbought) / (100 - self._rsi_overbought) + 0.3, 0.9)
            await self.emit_signal(Signal(
                strategy=self.name, symbol=symbol, side=Side.SELL, price=price,
                size=round(size, 6), confidence=round(confidence, 2), order_type=OrderType.LIMIT,
                reason=f"BB overbought: price={price:.4f} above upper={upper:.4f}, RSI={rsi:.1f}",
                metadata={"bb_upper": upper, "bb_lower": lower, "bb_mid": mean, "rsi": rsi},
            ))
            self._last_signal_tick[symbol] = self._tick_count

    @staticmethod
    def _calc_rsi(prices: list[float], period: int) -> float | None:
        if len(prices) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(len(prices) - period, len(prices)):
            delta = prices[i] - prices[i - 1]
            if delta > 0:
                gains += delta
            else:
                losses -= delta
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))
