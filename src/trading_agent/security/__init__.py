"""LLM trading agent security — prompt injection guard, spend limits, simulation, circuit breaker."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import structlog

from trading_agent.config import SecurityConfig
from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import Signal, Side

logger = structlog.get_logger(__name__)


INJECTION_PATTERNS = [
    r"ignore (previous|all) instructions",
    r"new (task|directive|instruction)",
    r"system prompt",
    r"send .{0,50} to 0x[0-9a-fA-F]{40}",
    r"transfer .{0,50} to",
    r"approve .{0,50} for",
    r"send .{0,50} to (bc1|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|T[XYZa-km-zA-HJ-NP-Z1-9]{33})",
    r"__import__\(",
    r"eval\(",
    r"exec\(",
]


class SpendLimitError(Exception):
    pass


class CircuitBreakerError(Exception):
    pass


class InjectionError(Exception):
    pass


class SlippageError(Exception):
    pass


@dataclass
class SpendRecord:
    amount_usdt: Decimal
    timestamp: float
    tx_id: str


@dataclass
class AuditEntry:
    timestamp: datetime
    action: str
    details: dict
    source: str


@dataclass
class CircuitBreakerState:
    """Tracks circuit breaker state for risk controls."""

    consecutive_losses: int = 0
    max_consecutive_losses: int = 3
    max_hourly_loss_pct: float = 0.05
    hour_start_value: float = 0.0
    hour_start_time: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def reset_hour(self, portfolio_value: float) -> None:
        self.hour_start_value = portfolio_value
        self.hour_start_time = time.time()

    def record_loss(self) -> None:
        self.consecutive_losses += 1

    def record_gain(self) -> None:
        self.consecutive_losses = 0

    def check(self, portfolio_value: float) -> tuple[bool, str]:
        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"Too many consecutive losses ({self.consecutive_losses})"

        if self.hour_start_value <= 0:
            return True, ""

        hourly_pnl = (portfolio_value - self.hour_start_value) / self.hour_start_value
        if hourly_pnl < -self.max_hourly_loss_pct:
            return False, f"Hourly PnL {hourly_pnl:.1%} below threshold {-self.max_hourly_loss_pct:.1%}"

        return True, ""


class SpendLimitGuard:
    """Hard spend limits — enforced independently from model output."""

    def __init__(self, max_single_tx: Decimal, max_daily: Decimal) -> None:
        self._max_single = max_single_tx
        self._max_daily = max_daily
        self._records: list[SpendRecord] = []

    def check_and_record(self, amount_usdt: Decimal, tx_id: str = "") -> None:
        if amount_usdt > self._max_single:
            raise SpendLimitError(f"Single tx ${amount_usdt} exceeds max ${self._max_single}")

        daily = self._get_24h_spend()
        if daily + amount_usdt > self._max_daily:
            raise SpendLimitError(f"Daily limit: ${daily} + ${amount_usdt} > ${self._max_daily}")

        self._record_spend(amount_usdt, tx_id)

    def _get_24h_spend(self) -> Decimal:
        cutoff = time.time() - 86400
        return sum(r.amount_usdt for r in self._records if r.timestamp >= cutoff)

    def _record_spend(self, amount_usdt: Decimal, tx_id: str) -> None:
        self._records.append(SpendRecord(amount_usdt=amount_usdt, timestamp=time.time(), tx_id=tx_id))

    def reset(self) -> None:
        self._records.clear()


class SecurityManager:
    """Orchestrates all security controls for the trading agent."""

    def __init__(self, config: SecurityConfig, event_bus: EventBus) -> None:
        self._config = config
        self._bus = event_bus
        self._spend_guard = SpendLimitGuard(
            max_single_tx=Decimal(str(config.max_single_tx_usdt)),
            max_daily=Decimal(str(config.max_daily_spend_usdt)),
        )
        self._circuit_breaker = CircuitBreakerState(
            max_consecutive_losses=config.max_consecutive_losses,
            max_hourly_loss_pct=config.max_hourly_loss_pct,
        )
        self._audit_log: list[AuditEntry] = []
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    def sanitize_input(self, text: str) -> str:
        if not self._config.injection_guard_enabled:
            return text
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                raise InjectionError(f"Potential prompt injection detected: {text[:100]}")
        return text

    def check_signal(self, signal: Signal, portfolio_value: float) -> Signal:
        if self._halted:
            raise CircuitBreakerError("Trading halted by circuit breaker")

        self.sanitize_input(signal.reason)
        for key, value in signal.metadata.items():
            if isinstance(value, str):
                self.sanitize_input(value)

        amount = Decimal(str(signal.price * signal.size))
        try:
            self._spend_guard.check_and_record(amount)
        except SpendLimitError:
            if signal.size > 0:
                max_size = float(self._config.max_single_tx_usdt) / signal.price
                if max_size < signal.size:
                    signal = signal.model_copy(update={"size": max_size})
                    logger.info("security_size_reduced", original=signal.size, reduced=max_size)

        passed, reason = self._circuit_breaker.check(portfolio_value)
        if not passed:
            self._halt_circuit_breaker(reason)
            raise CircuitBreakerError(reason)

        return signal

    def record_trade_result(self, pnl: float, details: dict) -> None:
        if pnl < 0:
            self._circuit_breaker.record_loss()
        else:
            self._circuit_breaker.record_gain()

        if self._config.audit_log_enabled:
            self._audit_log.append(
                AuditEntry(timestamp=datetime.now(), action="trade_executed", details=details, source="security_manager")
            )

    def set_portfolio_value(self, value: float) -> None:
        if self._circuit_breaker.hour_start_value <= 0:
            self._circuit_breaker.reset_hour(value)
        elif time.time() - self._circuit_breaker.hour_start_time > 3600:
            self._circuit_breaker.reset_hour(value)

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        return [
            {"timestamp": e.timestamp.isoformat(), "action": e.action, "details": e.details, "source": e.source}
            for e in self._audit_log[-limit:]
        ]

    async def _halt_circuit_breaker(self, reason: str) -> None:
        self._halted = True
        self._circuit_breaker.halted = True
        self._circuit_breaker.halt_reason = reason
        await self._bus.publish(
            Event(
                type=EventType.CIRCUIT_BREAKER_TRIPPED,
                data={"reason": reason, "halted": True},
                source="security_manager",
            )
        )
        logger.warning("circuit_breaker_tripped", reason=reason)

    async def resume(self) -> None:
        self._halted = False
        self._circuit_breaker.halted = False
        self._circuit_breaker.consecutive_losses = 0
        logger.info("circuit_breaker_resumed")
