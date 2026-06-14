"""Entry point — wires all components together and runs the agent."""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

import click
import structlog

from trading_agent.config import AppConfig, load_config
from trading_agent.events import EventBus, EventType, Event
from trading_agent.execution import ExecutionEngine
from trading_agent.market_data import MarketDataService
from trading_agent.broker import BinanceClient, BinanceWebSocket
from trading_agent.llm import OllamaClient, MultiModelClient
from trading_agent.monitoring.dashboard import DashboardServer
from trading_agent.monitoring.metrics import MetricsCollector, TradeMetrics
from trading_agent.risk import RiskManager
from trading_agent.security import SecurityManager
from trading_agent.strategies.registry import StrategyRegistry

logger = structlog.get_logger(__name__)


async def run_agent(config: AppConfig) -> None:
    import structlog
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="ISO"), structlog.dev.ConsoleRenderer()],
        cache_logger_on_first_use=True,
    )
    logger.info("agent_starting", mode=config.trading.mode)

    event_bus = EventBus()
    metrics = MetricsCollector()
    binance_client = None
    dashboard = None
    market_data = None

    # Security manager
    security = SecurityManager(config.security, event_bus)

    # Market data service
    if config.simulation.enabled and not (config.binance.api_key and config.binance.api_secret):
        market_data = MarketDataService(event_bus, None, config.market_data)
        market_data.enable_simulation(config.simulation.symbols, config.simulation.initial_prices)
        logger.info("simulation_mode", symbols=config.simulation.symbols)
    else:
        binance_client = BinanceClient(
            api_key=config.binance.api_key,
            api_secret=config.binance.api_secret,
            testnet=config.binance.use_testnet,
        )
        try:
            await binance_client.initialize()
        except Exception as e:
            logger.error("binance_init_failed", error=str(e))
            market_data = MarketDataService(event_bus, None, config.market_data)
            market_data.enable_simulation(config.simulation.symbols, config.simulation.initial_prices)
        else:
            market_data = MarketDataService(event_bus, binance_client, config.market_data)

    await market_data.initialize(config.market_data.symbols)
    symbols = market_data.get_all_symbols()

    if not symbols:
        logger.error("no_symbols_found")
        return

    # Risk manager
    risk_manager = RiskManager(config.trading, config.security, event_bus)

    # Execution engine
    execution_engine = ExecutionEngine(
        config.trading, event_bus, binance_client, market_data, security
    )

    # LLM client (optional)
    llm_client = None
    if config.llm.enabled:
        if config.llm.provider == "ollama":
            ollama = OllamaClient(
                base_url=config.llm.base_url,
                model=config.llm.model,
                max_tokens=config.llm.max_tokens,
            )
            await ollama.initialize()
            llm_client = MultiModelClient([("ollama", ollama)])
            logger.info("llm_enabled", provider=config.llm.provider, model=config.llm.model)

    # Discover and instantiate strategies
    registry = StrategyRegistry()
    registry.discover()
    strategies = []

    for name, strategy_config in config.strategies.items():
        if not strategy_config.enabled:
            continue
        if name not in registry.list_strategies():
            logger.warning("strategy_not_found", name=name)
            continue

        try:
            strategy = registry.create(name, strategy_config.params, event_bus)

            if hasattr(strategy, "set_market_data"):
                strategy.set_market_data(market_data)
            if hasattr(strategy, "set_clients") and llm_client:
                strategy.set_clients(llm_client, market_data)

            strategies.append(strategy)
        except Exception as e:
            logger.error("strategy_init_error", name=name, error=str(e))

    # Set up WebSocket if using real Binance
    ws = None
    if binance_client and not config.simulation.enabled:
        ws = BinanceWebSocket()
        ticker_streams = BinanceWebSocket.build_ticker_stream(symbols)
        depth_streams = BinanceWebSocket.build_depth_stream(symbols)

        async def on_ticker(data: dict):
            symbol = data.get("s", "")
            if symbol:
                from trading_agent.models import Ticker
                ticker = Ticker(
                    symbol=symbol,
                    bid=float(data.get("b", 0)),
                    ask=float(data.get("a", 0)),
                    last=float(data.get("c", 0)),
                    volume_24h=float(data.get("v", 0)),
                    change_24h_pct=float(data.get("P", 0)),
                    high_24h=float(data.get("h", 0)),
                    low_24h=float(data.get("l", 0)),
                    timestamp=data.get("E", 0),
                )
                await event_bus.publish(Event(type=EventType.TICKER_UPDATE, data={"symbol": symbol, "ticker": ticker}))

        async def on_depth(data: dict):
            symbol = data.get("s", "")
            if symbol:
                from trading_agent.models import OrderBookLevel
                bids_raw = data.get("b", [])
                asks_raw = data.get("a", [])
                bids = [OrderBookLevel(price=float(b[0]), size=float(b[1])) for b in (bids_raw or [])]
                asks = [OrderBookLevel(price=float(a[0]), size=float(a[1])) for a in (asks_raw or [])]
                await event_bus.publish(Event(type=EventType.ORDERBOOK_UPDATE, data={
                    "symbol": symbol, "bids": [(b.price, b.size) for b in bids],
                    "asks": [(a.price, a.size) for a in asks],
                }))

        ws.on("24hrTicker", on_ticker)
        ws.on("depthUpdate", on_depth)
        asyncio.create_task(ws.connect(ticker_streams + depth_streams))

    # Start monitoring dashboard
    if config.monitoring.enabled:
        dashboard = DashboardServer(
            metrics,
            host=config.monitoring.host,
            port=config.monitoring.port,
        )
        await dashboard.start()

    # Metrics collection
    async def collect_metrics(event: Event) -> None:
        if event.type == EventType.ORDER_FILLED:
            trade_data = event.data.get("trade")
            if trade_data:
                from trading_agent.models import Trade
                if isinstance(trade_data, Trade):
                    metrics.record_trade(TradeMetrics(
                        symbol=trade_data.symbol,
                        strategy=trade_data.strategy,
                        side=trade_data.side.value,
                        price=trade_data.price,
                        size=trade_data.size,
                        value_usdt=trade_data.price * trade_data.size,
                        timestamp=datetime.fromtimestamp(trade_data.timestamp),
                    ))
        elif event.type == EventType.HEARTBEAT:
            metrics.update_system_metrics()

    event_bus.subscribe(EventType.ORDER_FILLED, collect_metrics)
    event_bus.subscribe(EventType.HEARTBEAT, collect_metrics)

    # Start strategies
    for strategy in strategies:
        await strategy.start()
        logger.info("strategy_started", name=strategy.name)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _shutdown(*_args):
        logger.info("shutdown_requested")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    # Heartbeat task
    async def heartbeat():
        while not shutdown_event.is_set():
            await event_bus.publish(Event(type=EventType.HEARTBEAT, data={}))
            # Update portfolio value from risk manager
            risk_manager.set_portfolio_value(
                execution_engine._paper.get_balance() if execution_engine._paper else 0
            )
            security.set_portfolio_value(
                execution_engine._paper.get_balance() if execution_engine._paper else 0
            )
            await asyncio.sleep(config.trading.tick_interval_seconds)

    # Run everything
    tasks = [
        asyncio.create_task(event_bus.run(), name="event_bus"),
        asyncio.create_task(heartbeat(), name="heartbeat"),
    ]

    logger.info(
        "agent_running",
        mode=config.trading.mode,
        symbols=len(symbols),
        strategies=[s.name for s in strategies],
    )

    await shutdown_event.wait()

    # Cleanup
    logger.info("agent_shutting_down")
    if dashboard:
        await dashboard.stop()
    if ws:
        await ws.stop()
    for strategy in strategies:
        await strategy.stop()
    if binance_client:
        await binance_client.close()
    await event_bus.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("agent_stopped")


def setup_logging():
    import structlog
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="ISO"), structlog.dev.ConsoleRenderer()],
        cache_logger_on_first_use=True,
    )


@click.command()
@click.option("--config", "-c", default="config/default.yaml", help="Path to config file")
def main(config: str) -> None:
    config_path = Path(config)
    if not config_path.exists():
        click.echo(f"Config file not found: {config_path}")
        sys.exit(1)
    app_config = load_config(config_path)
    asyncio.run(run_agent(app_config))


if __name__ == "__main__":
    main()
