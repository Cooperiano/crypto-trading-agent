"""Configuration management with Pydantic Settings + YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TradingConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    max_portfolio_pct_per_trade: float = 0.02
    max_position_per_symbol_usdt: float = 500.0
    daily_loss_limit_usdt: float = 200.0
    stop_loss_pct: float = 0.10
    tick_interval_seconds: float = 5.0
    quote_currency: str = "USDT"


class BinanceConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    testnet_api_key: str = ""
    testnet_api_secret: str = ""
    use_testnet: bool = True


class LLMConfig(BaseModel):
    enabled: bool = False
    provider: Literal["ollama", "openai", "anthropic", "hybrid"] = "ollama"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    model: str = "llama3"
    max_tokens: int = 2048
    analysis_interval_seconds: float = 300.0
    trading_mode: Literal["advisor", "autonomous", "hybrid"] = "hybrid"
    advisor_weight: float = 0.3


class StrategyConfig(BaseModel):
    enabled: bool = True
    trading_mode: Literal["advisor", "autonomous", "hybrid"] | None = None
    params: dict = Field(default_factory=dict)


class MarketDataConfig(BaseModel):
    primary_source: Literal["binance", "coingecko", "yahoo"] = "binance"
    fallback_sources: list[str] = Field(default_factory=lambda: ["coingecko", "yahoo"])
    symbols: list[str] = Field(default_factory=list)
    min_volume_24h_usdt: float = 1000000.0
    max_symbols: int = 20
    update_interval_seconds: float = 1.0


class SimulationConfig(BaseModel):
    enabled: bool = False
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    initial_prices: dict[str, float] = Field(default_factory=dict)


class MonitoringConfig(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    dashboard_type: Literal["python", "typescript"] = "python"


class SecurityConfig(BaseModel):
    max_single_tx_usdt: float = 500.0
    max_daily_spend_usdt: float = 2000.0
    injection_guard_enabled: bool = True
    simulation_before_send: bool = True
    circuit_breaker_enabled: bool = True
    max_consecutive_losses: int = 3
    max_hourly_loss_pct: float = 0.05
    min_amount_out_required: bool = True
    audit_log_enabled: bool = True


class AppConfig(BaseSettings):
    trading: TradingConfig = Field(default_factory=TradingConfig)
    binance: BinanceConfig = Field(default_factory=BinanceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    model_config = {"env_prefix": "TRADE_", "env_nested_delimiter": "__"}


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        return AppConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    strategies_raw = raw.pop("strategies", {})
    strategies = {}
    for name, params in strategies_raw.items():
        if isinstance(params, dict):
            enabled = params.pop("enabled", True)
            trading_mode = params.pop("trading_mode", None)
            strategies[name] = StrategyConfig(enabled=enabled, trading_mode=trading_mode, params=params)
        else:
            strategies[name] = StrategyConfig()

    return AppConfig(strategies=strategies, **raw)
