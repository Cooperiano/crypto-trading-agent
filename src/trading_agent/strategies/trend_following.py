"""Trend-following strategy — classic multi-timeframe trend detection."""

from __future__ import annotations

import structlog
from collections import deque

from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Side, Signal, OrderType
from trading_agent.strategies.base import BaseStrategy

logger = structlog.get_logger(__name__)


class TrendFollowingStrategy(BaseStrategy):
    """Classic trend-following using EMA crossover and ADX confirmation.

    Config params:
        ema_fast: int = 12 — fast EMA period
        ema_slow: int = 26 — slow EMA period
        adx_period: int = 14 — ADX calculation period
        adx_threshold: float = 25 — minimum ADX for trend strength
        max_position_usdt: float = 200.0
        cooldown_ticks: int = 10 — minimum ticks between signals per symbol
    """

    def __init__(self, name: str, config: dict, event_bus: EventBus) -> None:
        super().__init__(name, config, event_bus)
        self._ema_fast: int = config.get("ema_fast", 12)
        self._ema_slow: int = config.get("ema_slow", 26)
        self._adx_period: int = config.get("adx_period", 14)
        self._adx_threshold: float = config.get("adx_threshold", 25.0)
        self._max_position_usdt: float = config.get("max_position_usdt", 200.0)
        self._cooldown_ticks: int = config.get("cooldown_ticks", 10)
        self._prices: dict[str, deque[float]] = {}
        self._last_signal_tick: dict[str, int] = {}
        self._tick_count: int = 0

    async def start(self) -> None:
        self._bus.subscribe(EventType.ORDERBOOK_UPDATE, self.on_event)
        self._bus.subscribe(EventType.TICKER_UPDATE, self.on_event)
        logger.info("trend_following_started", ema_fast=self._ema_fast, ema_slow=self._ema_slow)

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
            self._prices[symbol] = deque(maxlen=max(self._ema_slow * 2, 100))
        self._prices[symbol].append(price)

        self._tick_count += 1
        last_tick = self._last_signal_tick.get(symbol, -self._cooldown_ticks)
        if self._tick_count - last_tick < self._cooldown_ticks:
            return

        prices = self._prices[symbol]
        if len(prices) < self._ema_slow:
            return

        prices_list = list(prices)
        fast_ema = self._calc_ema(prices_list, self._ema_fast)
        slow_ema = self._calc_ema(prices_list, self._ema_slow)
        adx = self._calc_adx(prices_list[-self._adx_period * 2:], self._adx_period)

        prev_fast = self._calc_ema(prices_list[:-1], self._ema_fast)
        prev_slow = self._calc_ema(prices_list[:-1], self._ema_slow)

        if adx < self._adx_threshold:
            return

        if fast_ema > slow_ema and prev_fast <= prev_slow:
            size = self._max_position_usdt / price
            await self.emit_signal(Signal(
                strategy=self.name, symbol=symbol, side=Side.BUY, price=price,
                size=round(size, 6), confidence=min(adx / 50, 0.9), order_type=OrderType.LIMIT,
                reason=f"EMA crossover: fast>{self._ema_fast} crossed above slow>{self._ema_slow}, ADX={adx:.1f}",
                metadata={"adx": adx, "fast_ema": fast_ema, "slow_ema": slow_ema},
            ))
            self._last_signal_tick[symbol] = self._tick_count

        elif fast_ema < slow_ema and prev_fast >= prev_slow:
            size = self._max_position_usdt / price
            await self.emit_signal(Signal(
                strategy=self.name, symbol=symbol, side=Side.SELL, price=price,
                size=round(size, 6), confidence=min(adx / 50, 0.9), order_type=OrderType.LIMIT,
                reason=f"EMA crossover: fast>{self._ema_fast} crossed below slow>{self._ema_slow}, ADX={adx:.1f}",
                metadata={"adx": adx, "fast_ema": fast_ema, "slow_ema": slow_ema},
            ))
            self._last_signal_tick[symbol] = self._tick_count

    @staticmethod
    def _calc_ema(prices: list[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1]
        alpha = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * alpha + ema * (1 - alpha)
        return ema

    @staticmethod
    def _calc_adx(prices: list[float], period: int) -> float:
        if len(prices) < period + 1:
            return 0.0
        tr_values = []
        for i in range(1, len(prices)):
            tr = abs(prices[i] - prices[i - 1])
            tr_values.append(tr)
        if not tr_values:
            return 0.0
        atr = sum(tr_values[-period:]) / period
        up = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
        down = len(prices) - 1 - up
        if up + down == 0:
            return 0.0
        dx = abs(up - down) / (up + down) * 100
        return dx
