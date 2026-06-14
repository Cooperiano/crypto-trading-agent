"""Metrics collection and tracking system."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TradeMetrics:
    symbol: str
    strategy: str
    side: str
    price: float
    size: float
    value_usdt: float
    timestamp: datetime
    execution_time_ms: float | None = None
    pnl_usdt: float | None = None


@dataclass
class StrategyMetrics:
    name: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_volume_usdt: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_usdt: float = 0.0
    win_rate: float = 0.0

    def update_win_rate(self) -> None:
        total = self.winning_trades + self.losing_trades
        self.win_rate = self.winning_trades / total if total > 0 else 0.0


@dataclass
class SystemMetrics:
    uptime_seconds: float = 0.0
    websocket_connected: bool = False
    events_processed: int = 0
    last_heartbeat: datetime | None = None
    start_time: datetime = field(default_factory=datetime.now)


class MetricsCollector:
    def __init__(self) -> None:
        self._trades: list[TradeMetrics] = []
        self._strategy_metrics: dict[str, StrategyMetrics] = {}
        self._system_metrics = SystemMetrics()
        self._symbol_metrics: dict[str, dict] = defaultdict(lambda: {"trades": 0, "volume": 0.0, "pnl": 0.0})
        self._hourly_pnl: dict[str, float] = defaultdict(float)
        self._daily_pnl: dict[str, float] = defaultdict(float)
        self._start_time = time.time()

    def record_trade(self, trade: TradeMetrics) -> None:
        self._trades.append(trade)
        if trade.strategy not in self._strategy_metrics:
            self._strategy_metrics[trade.strategy] = StrategyMetrics(name=trade.strategy)
        sm = self._strategy_metrics[trade.strategy]
        sm.total_trades += 1
        sm.total_volume_usdt += trade.value_usdt
        if trade.pnl_usdt is not None:
            sm.total_pnl_usdt += trade.pnl_usdt
            if trade.pnl_usdt > 0:
                sm.winning_trades += 1
            elif trade.pnl_usdt < 0:
                sm.losing_trades += 1
            sm.update_win_rate()
        self._symbol_metrics[trade.symbol]["trades"] += 1
        self._symbol_metrics[trade.symbol]["volume"] += trade.value_usdt
        if trade.pnl_usdt is not None:
            self._symbol_metrics[trade.symbol]["pnl"] += trade.pnl_usdt
        hour_key = trade.timestamp.strftime("%Y-%m-%d-%H")
        day_key = trade.timestamp.strftime("%Y-%m-%d")
        if trade.pnl_usdt is not None:
            self._hourly_pnl[hour_key] += trade.pnl_usdt
            self._daily_pnl[day_key] += trade.pnl_usdt

    def get_pnl_summary(self) -> dict:
        total_pnl = sum(m.total_pnl_usdt for m in self._strategy_metrics.values())
        return {"total_pnl_usdt": total_pnl, "total_trades": sum(m.total_trades for m in self._strategy_metrics.values()),
                "total_volume_usdt": sum(m.total_volume_usdt for m in self._strategy_metrics.values()),
                "hourly_pnl": dict(self._hourly_pnl), "daily_pnl": dict(self._daily_pnl)}

    def get_strategy_metrics(self) -> dict[str, StrategyMetrics]:
        return dict(self._strategy_metrics)

    def get_system_metrics(self) -> SystemMetrics:
        self._system_metrics.uptime_seconds = time.time() - self._start_time
        return self._system_metrics

    def get_recent_trades(self, limit: int = 50) -> list[TradeMetrics]:
        return self._trades[-limit:]

    def export_metrics(self) -> dict:
        return {"system": {"uptime_seconds": self._system_metrics.uptime_seconds,
            "start_time": self._system_metrics.start_time.isoformat()},
            "strategies": {name: {"total_trades": m.total_trades, "winning_trades": m.winning_trades,
                "losing_trades": m.losing_trades, "total_pnl_usdt": m.total_pnl_usdt,
                "total_volume_usdt": m.total_volume_usdt, "win_rate": m.win_rate}
                for name, m in self._strategy_metrics.items()},
            "pnl_summary": self.get_pnl_summary(), "symbol_count": len(self._symbol_metrics)}

    def update_system_metrics(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self._system_metrics, key):
                setattr(self._system_metrics, key, value)
        self._system_metrics.uptime_seconds = time.time() - self._start_time
