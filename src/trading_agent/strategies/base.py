"""Base strategy abstract class and registry — the plugin contract for all strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod

import structlog

from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Signal

logger = structlog.get_logger(__name__)


class BaseStrategy(ABC):
    def __init__(self, name: str, config: dict, event_bus: EventBus) -> None:
        self.name = name
        self.config = config
        self._bus = event_bus
        self._enabled = True

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def on_event(self, event: Event) -> None:
        ...

    async def emit_signal(self, signal: Signal) -> None:
        logger.info(
            "strategy_signal",
            strategy=self.name,
            side=signal.side.value,
            symbol=signal.symbol,
            price=signal.price,
            size=signal.size,
            confidence=signal.confidence,
            reason=signal.reason,
        )
        await self._bus.publish(
            Event(
                type=EventType.STRATEGY_SIGNAL,
                data={"signal": signal},
                source=self.name,
            )
        )
