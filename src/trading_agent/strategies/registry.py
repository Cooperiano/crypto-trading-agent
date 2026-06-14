"""Strategy plugin registry — discovers and instantiates strategies."""

from __future__ import annotations

import importlib
import pkgutil

import structlog

from trading_agent.events import EventBus
from trading_agent.strategies.base import BaseStrategy

logger = structlog.get_logger(__name__)

STRATEGY_ALIASES: dict[str, str] = {
    "trend_following": "TrendFollowingStrategy",
    "mean_reversion": "MeanReversionStrategy",
    "llm": "LLMStrategy",
    "grid": "GridStrategy",
}


class StrategyRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, type[BaseStrategy]] = {}
        self._aliases: dict[str, str] = {}

    def discover(self) -> None:
        strategies_pkg = importlib.import_module("trading_agent.strategies")
        for _, module_name, _ in pkgutil.iter_modules(strategies_pkg.__path__):
            if module_name in ("base", "registry"):
                continue
            try:
                module = importlib.import_module(f"trading_agent.strategies.{module_name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, BaseStrategy) and attr is not BaseStrategy:
                        self._registry[attr_name] = attr
                        kebab_name = self._to_kebab_case(attr_name)
                        self._aliases[kebab_name] = attr_name
                        logger.debug("strategy_discovered", name=attr_name, alias=kebab_name)
            except Exception as e:
                logger.warning("strategy_discovery_error", module=module_name, error=str(e))

        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group="trading_agent.strategies"):
                self._registry[ep.name] = ep.load()
                logger.debug("strategy_loaded_entrypoint", name=ep.name)
        except Exception:
            pass

        logger.info("strategies_discovered", count=len(self._registry), names=list(self._registry.keys()))

    def _to_kebab_case(self, name: str) -> str:
        result = []
        for i, c in enumerate(name):
            if c.isupper() and i > 0:
                result.append("_")
            result.append(c.lower())
        return "".join(result).replace("_strategy", "").replace("_", "-")

    def create(self, name: str, config: dict, event_bus: EventBus) -> BaseStrategy:
        cls = self._registry.get(name)
        if not cls and name in self._aliases:
            cls = self._registry.get(self._aliases[name])
        if not cls and name in STRATEGY_ALIASES:
            cls = self._registry.get(STRATEGY_ALIASES[name])

        if not cls:
            available = list(self._registry.keys()) + list(self._aliases.keys())
            raise ValueError(f"Strategy '{name}' not found. Available: {available}")

        instance_name = name if "-" in name or "_" in name else self._to_kebab_case(name)
        return cls(name=instance_name, config=config, event_bus=event_bus)

    def list_strategies(self) -> list[str]:
        return list(set(list(self._registry.keys()) + list(self._aliases.keys()) + list(STRATEGY_ALIASES.keys())))
