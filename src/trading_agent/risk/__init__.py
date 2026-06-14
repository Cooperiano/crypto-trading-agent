"""Risk manager — pre-trade checks, position sizing, drawdown, circuit breakers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

import structlog

from trading_agent.config import TradingConfig, SecurityConfig
from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Position, Side, Signal, Trade

logger = structlog.get_logger(__name__)


@dataclass
class RiskLimits:
    max_portfolio_pct_per_trade: float = 0.02
    max_position_per_symbol_usdt: float = 500.0
    daily_loss_limit_usdt: float = 200.0
    stop_loss_pct: float = 0.10
    max_drawdown_pct: float = 0.15
    max_open_positions: int = 10
    trailing_stop_pct: float = 0.05
    volatility_adjustment: bool = True
    kelly_fraction: float = 0.25


@dataclass
class RiskAlert:
    timestamp: datetime
    type: str
    message: str
    value: float
    limit: float


@dataclass
class DrawdownTracker:
    peak_equity: float = 0.0
    current_equity: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0

    def update(self, equity: float) -> None:
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            self.current_drawdown = (self.peak_equity - equity) / self.peak_equity
            self.max_drawdown = max(self.max_drawdown, self.current_drawdown)


class RiskManager:
    def __init__(self, trading_config: TradingConfig, security_config: SecurityConfig, event_bus: EventBus,
                 limits: RiskLimits | None = None) -> None:
        self._trading_config = trading_config
        self._security_config = security_config
        self._bus = event_bus
        self._limits = limits or RiskLimits(max_portfolio_pct_per_trade=trading_config.max_portfolio_pct_per_trade,
            max_position_per_symbol_usdt=trading_config.max_position_per_symbol_usdt,
            daily_loss_limit_usdt=trading_config.daily_loss_limit_usdt, stop_loss_pct=trading_config.stop_loss_pct)
        self._positions: dict[str, Position] = {}
        self._daily_pnl: float = 0.0
        self._daily_reset_date: date = date.today()
        self._portfolio_value: float = 0.0
        self._drawdown_tracker = DrawdownTracker()
        self._alerts: list[RiskAlert] = []
        self._equity_curve: deque = deque(maxlen=1000)
        self._consecutive_losses: int = 0
        self._bus.subscribe(EventType.STRATEGY_SIGNAL, self._on_signal)
        self._bus.subscribe(EventType.ORDER_FILLED, self._on_fill)
        self._bus.subscribe(EventType.HEARTBEAT, self._on_heartbeat)

    def set_portfolio_value(self, value: float) -> None:
        self._portfolio_value = value
        self._drawdown_tracker.update(value)
        self._equity_curve.append((time.time(), value))

    def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_drawdown(self) -> tuple[float, float]:
        return self._drawdown_tracker.current_drawdown, self._drawdown_tracker.max_drawdown

    def get_consecutive_losses(self) -> int:
        return self._consecutive_losses

    def get_risk_metrics(self) -> dict:
        total_pos_value = sum(abs(p.size * p.avg_price) for p in self._positions.values())
        gross_exposure = total_pos_value / self._portfolio_value if self._portfolio_value > 0 else 0
        return {"daily_pnl_usdt": self._daily_pnl, "open_positions": len(self._positions),
                "gross_exposure_pct": gross_exposure, "current_drawdown_pct": self._drawdown_tracker.current_drawdown,
                "max_drawdown_pct": self._drawdown_tracker.max_drawdown, "portfolio_value_usdt": self._portfolio_value,
                "total_position_value_usdt": total_pos_value, "consecutive_losses": self._consecutive_losses}

    def check_stop_loss(self, symbol: str, current_price: float) -> Signal | None:
        pos = self._positions.get(symbol)
        if not pos or pos.side != Side.BUY:
            return None
        pos.update_pnl(current_price)
        if pos.size > 0 and pos.avg_price > 0:
            loss_pct = -pos.unrealized_pnl / (pos.avg_price * pos.size)
        else:
            loss_pct = 0
        if loss_pct >= self._limits.stop_loss_pct:
            return Signal(strategy="risk_manager", symbol=symbol, side=Side.SELL, price=current_price,
                          size=pos.size, confidence=1.0, reason=f"stop_loss at {loss_pct:.1%}")
        if self._limits.trailing_stop_pct > 0 and current_price <= pos.max_price * (1 - self._limits.trailing_stop_pct):
            return Signal(strategy="risk_manager", symbol=symbol, side=Side.SELL, price=current_price,
                          size=pos.size, confidence=1.0, reason=f"trailing_stop at {current_price:.4f}")
        return None

    async def _on_signal(self, event: Event) -> None:
        signal: Signal = event.data["signal"]
        self._maybe_reset_daily()
        approval, reason = await self._evaluate_signal(signal)
        if not approval:
            logger.warning("risk_rejected", strategy=signal.strategy, reason=reason)
            await self._emit_alert("SIGNAL_REJECTED", reason, signal.price * signal.size)
            return
        sized = self._apply_position_sizing(signal)
        if sized.size <= 0:
            return
        logger.info("risk_approved", strategy=signal.strategy, side=signal.side.value, symbol=signal.symbol)
        await self._bus.publish(Event(type=EventType.ORDER_SUBMITTED, data={"signal": sized}, source="risk_manager"))

    async def _on_fill(self, event: Event) -> None:
        trade: Trade = event.data["trade"]
        pos = self._positions.get(trade.symbol)
        if pos:
            if trade.side == pos.side:
                total_cost = pos.avg_price * pos.size + trade.price * trade.size
                pos.size += trade.size
                pos.avg_price = total_cost / pos.size if pos.size > 0 else 0
            else:
                realized = ((trade.price - pos.avg_price) if pos.side == Side.BUY else (pos.avg_price - trade.price)) * min(trade.size, pos.size)
                self._daily_pnl += realized
                if realized < 0:
                    self._consecutive_losses += 1
                else:
                    self._consecutive_losses = 0
                pos.size -= trade.size
                if pos.size <= 0:
                    del self._positions[trade.symbol]
                    return
                pos.avg_price = trade.price
        else:
            self._positions[trade.symbol] = Position(symbol=trade.symbol, side=trade.side, size=trade.size, avg_price=trade.price)
            logger.info("position_opened", symbol=trade.symbol, side=trade.side.value)

    async def _on_heartbeat(self, event: Event) -> None:
        await self._check_drawdown_limit()
        await self._check_position_limits()
        await self._check_consecutive_losses()

    async def _evaluate_signal(self, signal: Signal) -> tuple[bool, str]:
        if self._daily_pnl <= -self._limits.daily_loss_limit_usdt:
            return False, f"Daily loss limit: ${self._daily_pnl:.2f}"
        if len(self._positions) >= self._limits.max_open_positions:
            return False, f"Max positions: {len(self._positions)}"
        if self._drawdown_tracker.current_drawdown >= self._limits.max_drawdown_pct:
            return False, f"Max drawdown: {self._drawdown_tracker.current_drawdown:.1%}"
        return True, "approved"

    def _apply_position_sizing(self, signal: Signal) -> Signal:
        base_size = signal.size
        max_trade_value = self._portfolio_value * self._limits.max_portfolio_pct_per_trade
        if max_trade_value > 0:
            base_size = min(base_size, max_trade_value / signal.price)
        existing = self._positions.get(signal.symbol)
        if existing:
            remaining = self._limits.max_position_per_symbol_usdt - abs(existing.size * existing.avg_price)
            base_size = min(base_size, max(0, remaining / signal.price)) if remaining > 0 else 0
        if self._limits.kelly_fraction > 0 and signal.confidence > 0 and self._portfolio_value > 0:
            kelly = (signal.confidence * 2 - 1) * self._limits.kelly_fraction
            if kelly > 0:
                base_size = min(base_size, self._portfolio_value * kelly / signal.price)
        return signal.model_copy(update={"size": max(0, base_size)})

    async def _check_drawdown_limit(self) -> None:
        if self._drawdown_tracker.current_drawdown >= self._limits.max_drawdown_pct:
            await self._emit_alert("MAX_DRAWDOWN", f"Drawdown: {self._drawdown_tracker.current_drawdown:.1%}", self._drawdown_tracker.current_drawdown)

    async def _check_position_limits(self) -> None:
        total = sum(abs(p.size * p.avg_price) for p in self._positions.values())
        for sym, pos in self._positions.items():
            conc = abs(pos.size * pos.avg_price) / total if total > 0 else 0
            if conc > 0.3:
                await self._emit_alert("CONCENTRATION", f"{sym}: {conc:.1%}", conc)

    async def _check_consecutive_losses(self) -> None:
        if self._consecutive_losses >= self._security_config.max_consecutive_losses:
            await self._emit_alert("MAX_CONSECUTIVE_LOSSES", f"{self._consecutive_losses} consecutive", float(self._consecutive_losses))

    async def _emit_alert(self, alert_type: str, message: str, value: float) -> None:
        alert = RiskAlert(timestamp=datetime.now(), type=alert_type, message=message, value=value, limit=0.0)
        self._alerts.append(alert)
        await self._bus.publish(Event(type=EventType.RISK_ALERT, data={"alert": alert}, source="risk_manager"))
        logger.warning("risk_alert", type=alert_type, message=message)

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._daily_reset_date:
            logger.info("risk_daily_reset", prev_pnl=self._daily_pnl)
            self._daily_pnl = 0.0
            self._daily_reset_date = today
