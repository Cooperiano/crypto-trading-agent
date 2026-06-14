"""Backtesting engine for evaluating strategies on historical data."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import structlog

from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Side, Signal, Trade

logger = structlog.get_logger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 10000.0
    commission_rate: float = 0.001
    slippage_bps: float = 5.0
    position_size_fraction: float = 0.1
    max_positions: int = 5


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    profit_factor: float
    equity_curve: list[float]
    daily_returns: list[float]

    def to_dict(self) -> dict:
        return {
            "start_date": self.start_date, "end_date": self.end_date,
            "total_trades": self.total_trades, "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades, "win_rate": self.win_rate,
            "total_pnl": self.total_pnl, "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct, "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio, "profit_factor": self.profit_factor,
        }


@dataclass
class MarketSnapshot:
    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._config = config or BacktestConfig()
        self._snapshots: dict[str, list[MarketSnapshot]] = {}
        self._signals: list[Signal] = []

    def add_snapshots(self, symbol: str, snapshots: list[MarketSnapshot]) -> None:
        self._snapshots[symbol] = sorted(snapshots, key=lambda s: s.timestamp)

    def add_signal(self, signal: Signal) -> None:
        self._signals.append(signal)

    def run_backtest(self, strategy, strategy_config: dict | None = None) -> BacktestResult:
        event_bus = EventBus()
        strategy_instance = strategy(name="backtest", config=strategy_config or {}, event_bus=event_bus)
        signals: list[Signal] = []

        all_snapshots: list[MarketSnapshot] = []
        for symbol, snaps in self._snapshots.items():
            all_snapshots.extend(snaps)
        all_snapshots.sort(key=lambda s: s.timestamp)

        if not all_snapshots:
            raise ValueError("No market snapshots provided")

        equity = self._config.initial_capital
        peak_equity = equity
        max_drawdown = 0.0
        trades: list[dict] = []
        equity_curve = [equity]
        positions: dict[str, dict] = {}
        daily_pnl: dict[str, float] = {}

        async def collect_signals(event: Event) -> None:
            if event.type == EventType.STRATEGY_SIGNAL:
                signals.append(event.data["signal"])

        event_bus.subscribe(EventType.STRATEGY_SIGNAL, collect_signals)

        # Replay snapshots
        for snapshot in all_snapshots:
            mid = (snapshot.high + snapshot.low) / 2
            event = Event(
                type=EventType.ORDERBOOK_UPDATE,
                data={"symbol": snapshot.symbol, "mid": mid, "bid": snapshot.low, "ask": snapshot.high},
                timestamp=snapshot.timestamp,
            )

            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(strategy_instance.on_event(event))
                else:
                    loop.run_until_complete(strategy_instance.on_event(event))
            except Exception:
                pass

            # Process collected signals
            for signal in signals[:]:
                signal_value = signal.price * signal.size
                # Apply fees + slippage
                slip = signal.price * (self._config.slippage_bps / 10000)
                fill_price = signal.price + slip if signal.side == Side.BUY else signal.price - slip
                cost = fill_price * signal.size
                fee = cost * self._config.commission_rate

                if cost + fee > equity * self._config.position_size_fraction:
                    continue

                if signal.side == Side.BUY:
                    equity -= (cost + fee)
                    key = signal.symbol + "_long"
                    if key in positions:
                        p = positions[key]
                        total = p["size"] * p["avg_price"] + signal.size * fill_price
                        p["size"] += signal.size
                        p["avg_price"] = total / p["size"]
                    else:
                        positions[key] = {"size": signal.size, "avg_price": fill_price, "side": "BUY"}
                else:
                    equity += cost - fee
                    key = signal.symbol + "_long"
                    if key in positions:
                        p = positions[key]
                        pnl = (fill_price - p["avg_price"]) * min(signal.size, p["size"])
                        p["size"] -= signal.size
                        if p["size"] <= 0:
                            del positions[key]
                    else:
                        positions[signal.symbol + "_short"] = {"size": signal.size, "avg_price": fill_price, "side": "SELL"}

                trades.append({"timestamp": snapshot.timestamp, "symbol": signal.symbol, "side": signal.side.value,
                               "price": fill_price, "size": signal.size, "fee": fee, "pnl": pnl if 'pnl' in dir() else None})
                signals.remove(signal)

            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_drawdown = max(max_drawdown, dd)

            day = datetime.fromtimestamp(snapshot.timestamp / 1000).strftime("%Y-%m-%d")
            daily_pnl[day] = daily_pnl.get(day, 0.0)

        return self._compute_results(trades, equity, peak_equity, max_drawdown,
                                     equity_curve, daily_pnl, all_snapshots)

    def _compute_results(self, trades: list[dict], final_equity: float, peak_equity: float,
                         max_dd: float, equity_curve: list[float],
                         daily_pnl: dict[str, float],
                         snapshots: list[MarketSnapshot]) -> BacktestResult:
        winning = [t for t in trades if t.get("pnl", 0) > 0]
        losing = [t for t in trades if t.get("pnl", 0) < 0]
        win_rate = len(winning) / len(trades) if trades else 0

        total_pnl = sum(t.get("pnl", 0) for t in trades)
        return_pct = (final_equity - self._config.initial_capital) / self._config.initial_capital

        start_ts = snapshots[0].timestamp if snapshots else time.time() * 1000
        end_ts = snapshots[-1].timestamp if snapshots else time.time() * 1000

        daily_returns = list(daily_pnl.values())
        if daily_returns:
            avg_return = sum(daily_returns) / len(daily_returns)
            std = (sum((r - avg_return) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
            sharpe = (avg_return / std * (252 ** 0.5)) if std > 0 else 0
            down_returns = [r for r in daily_returns if r < 0]
            down_std = (sum(r ** 2 for r in down_returns) / len(down_returns)) ** 0.5 if down_returns else 0
            sortino = (avg_return / down_std * (252 ** 0.5)) if down_std > 0 else 0
        else:
            sharpe = 0
            sortino = 0

        gross_profit = sum(t.get("pnl", 0) for t in winning)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        return BacktestResult(
            start_date=datetime.fromtimestamp(start_ts / 1000).isoformat(),
            end_date=datetime.fromtimestamp(end_ts / 1000).isoformat(),
            total_trades=len(trades), winning_trades=len(winning), losing_trades=len(losing),
            win_rate=win_rate, total_pnl=total_pnl, return_pct=return_pct,
            max_drawdown_pct=max_dd, sharpe_ratio=sharpe, sortino_ratio=sortino,
            profit_factor=profit_factor, equity_curve=equity_curve, daily_returns=daily_returns,
        )
