"""LLM strategy — uses LLM for autonomous or advisor-driven trading signals."""

from __future__ import annotations

import structlog

from trading_agent.events import Event, EventBus, EventType
from trading_agent.llm import MultiModelClient
from trading_agent.models import Side, Signal, OrderType
from trading_agent.strategies.base import BaseStrategy

logger = structlog.get_logger(__name__)


class LLMStrategy(BaseStrategy):
    """Periodically analyzes markets using an LLM for trading signals.

    Config params:
        min_confidence: float = 0.6 — minimum confidence to emit signal
        max_position_usdt: float = 100.0
        analysis_interval_ticks: int = 120
        trading_mode: str = "hybrid" — "advisor", "autonomous", or "hybrid"
    """

    def __init__(self, name: str, config: dict, event_bus: EventBus) -> None:
        super().__init__(name, config, event_bus)
        self._min_confidence: float = config.get("min_confidence", 0.6)
        self._max_position_usdt: float = config.get("max_position_usdt", 100.0)
        self._analysis_interval_ticks: int = config.get("analysis_interval_ticks", 120)
        self._trading_mode: str = config.get("trading_mode", "hybrid")
        self._tick_count: int = 0
        self._llm: MultiModelClient | None = None
        self._market_data = None

    def set_clients(self, llm: MultiModelClient, market_data) -> None:
        self._llm = llm
        self._market_data = market_data

    async def start(self) -> None:
        self._bus.subscribe(EventType.HEARTBEAT, self.on_event)
        logger.info("llm_strategy_started", mode=self._trading_mode, interval=self._analysis_interval_ticks)

    async def stop(self) -> None:
        self._enabled = False

    async def on_event(self, event: Event) -> None:
        if not self._enabled or not self._llm or not self._market_data:
            return
        if event.type != EventType.HEARTBEAT:
            return

        self._tick_count += 1
        if self._tick_count % self._analysis_interval_ticks != 0:
            return

        symbols = self._market_data.get_all_symbols()
        for symbol in symbols:
            book = self._market_data.get_book(symbol)
            ticker = self._market_data.get_ticker(symbol)
            if not book or not book.mid:
                continue

            try:
                analysis, client_name = await self._llm.analyze_market(
                    symbol=symbol,
                    current_price=book.mid,
                    volume_24h=ticker.volume_24h if ticker else 0.0,
                )

                if analysis.direction == "neutral" or analysis.confidence < self._min_confidence:
                    continue

                side = Side.BUY if analysis.direction == "long" else Side.SELL
                price = book.best_ask if side == Side.BUY else (book.best_bid or book.mid)
                if not price:
                    continue

                size = min(self._max_position_usdt / price, analysis.confidence * self._max_position_usdt / price)

                await self.emit_signal(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    side=side,
                    price=round(price, 6),
                    size=round(size, 6),
                    confidence=analysis.confidence,
                    order_type=OrderType.LIMIT,
                    reason=f"LLM({client_name}): {analysis.reasoning[:100]}",
                    metadata={
                        "direction": analysis.direction,
                        "target_price": analysis.target_price,
                        "stop_loss_price": analysis.stop_loss_price,
                        "key_factors": analysis.key_factors,
                        "trading_mode": self._trading_mode,
                    },
                ))

                await self._bus.publish(Event(
                    type=EventType.LLM_ANALYSIS,
                    data={"symbol": symbol, "analysis": analysis.model_dump(), "client": client_name},
                    source=self.name,
                ))

            except Exception as e:
                logger.warning("llm_analysis_error", symbol=symbol, error=str(e))
