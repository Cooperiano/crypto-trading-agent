"""Async event bus for inter-module communication."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger(__name__)


class EventType(Enum):
    ORDERBOOK_UPDATE = auto()
    TICKER_UPDATE = auto()
    TRADE = auto()
    STRATEGY_SIGNAL = auto()
    ORDER_SUBMITTED = auto()
    ORDER_FILLED = auto()
    ORDER_CANCELLED = auto()
    ORDER_REJECTED = auto()
    ORDER_PARTIALLY_FILLED = auto()
    POSITION_UPDATED = auto()
    RISK_ALERT = auto()
    CIRCUIT_BREAKER_TRIPPED = auto()
    LLM_ANALYSIS = auto()
    HEARTBEAT = auto()
    MARKET_DATA_READY = auto()
    CONNECTION_LOST = auto()
    CONNECTION_RESTORED = auto()


@dataclass(frozen=True)
class Event:
    type: EventType
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = ""


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        if event_type in self._handlers:
            self._handlers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def run(self) -> None:
        self._running = True
        logger.info("event_bus_started")
        while self._running:
            try:
                event = await self._queue.get()
                handlers = self._handlers.get(event.type, [])
                if not handlers:
                    continue
                results = await asyncio.gather(
                    *(self._safe_call(h, event) for h in handlers),
                    return_exceptions=True,
                )
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error(
                            "handler_error",
                            event_type=event.type.name,
                            handler=handlers[i].__qualname__,
                            error=str(result),
                        )
            except asyncio.CancelledError:
                break
        logger.info("event_bus_stopped")

    async def stop(self) -> None:
        self._running = False

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        await handler(event)
