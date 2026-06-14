# trading-agent

Modular async cryptocurrency trading agent for Binance, with optional LLM-assisted
decision making. Designed for safe paper/simulation trading first, with hard guardrails
before any real order is ever sent.

## Features

- **Async event-driven core** — single `EventBus` routes market data, strategy signals,
  order events, and risk alerts between loosely-coupled modules.
- **Paper / live modes** — defaults to paper trading. With no API keys configured the agent
  automatically falls back to a built-in price simulator.
- **Pluggable strategies** — strategies are auto-discovered and registered. Add a class that
  subclasses `BaseStrategy`, drop it in `strategies/`, and it shows up in config by name.
- **Built-in strategies** — `trend_following` (EMA cross + ADX filter) and `mean_reversion`
  (Bollinger Bands + RSI).
- **Optional LLM advisor** — pluggable LLM client (Ollama by default; OpenAI/Anthropic
  provider slots in config) operating in `advisor`, `autonomous`, or `hybrid` trading modes.
- **Defense in depth** — separate `RiskManager` (position sizing, daily-loss limits, stop
  loss) and `SecurityManager` (per-tx cap, daily spend cap, circuit breaker, injection guard,
  pre-send simulation, audit log).
- **Monitoring dashboard** — built-in HTTP dashboard (default `:8080`) with live metrics.

## Quick start

```bash
# 1. Create a venv and install
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Run in paper/simulation mode (no API keys needed)
trading-agent --config config/default.yaml
```

The default config enables the price simulator (`simulation.enabled: true`) so the agent
runs end-to-end without any exchange credentials. Open <http://localhost:8080> for the
dashboard.

## Configuration

Config is layered: YAML file → environment variables (env prefix `TRADE_`, nested with `__`).

```bash
# YAML keys map to env vars via TRADE_<SECTION>__<KEY>
export TRADE_BINANCE__API_KEY=...
export TRADE_BINANCE__API_SECRET=...
export TRADE_LLM__API_KEY=...
```

| File | Purpose |
|------|---------|
| `config/default.yaml` | Paper trading with the price simulator. Safe local default. |
| `config/live.yaml`   | Tighter risk limits, LLM advisor enabled. Closer to real use. |
| `.env.example`       | All supported environment variables. |

Key sections: `trading`, `binance`, `llm`, `market_data`, `simulation`, `strategies`,
`monitoring`, `security`. See [config/default.yaml](config/default.yaml) for the full schema.

### Adding a strategy

1. Create `src/trading_agent/strategies/my_strategy.py` with a `MyStrategy(BaseStrategy)`.
2. Implement `start`, `stop`, and `on_event`; call `self.emit_signal(...)` to publish.
3. Enable it in config under `strategies.my_strategy:`. The registry auto-discovers and
   aliases it (kebab-case: `my-strategy`).

## Development

```bash
ruff check src/                # lint
mypy --ignore-missing-imports src/trading_agent/   # type check
pytest                         # tests
```

## Project layout

```
src/trading_agent/
├── __main__.py        # entry point — wires all components, runs the agent
├── config.py          # Pydantic Settings config models + YAML loader
├── models.py          # Ticker, OrderBook, Signal, Trade, Position dataclasses
├── events.py          # EventBus + EventType
├── broker/            # BinanceClient (REST) + BinanceWebSocket (streaming)
├── market_data/       # MarketDataService with simulation fallback
├── strategies/        # BaseStrategy + registry + built-in strategies
├── execution/         # ExecutionEngine — turns signals into orders
├── risk/              # RiskManager — position sizing, limits, stop loss
├── security/          # SecurityManager — caps, circuit breaker, audit
├── llm/               # OllamaClient + MultiModelClient
├── monitoring/        # DashboardServer + MetricsCollector
└── backtesting/       # (reserved)
```

## Safety

This agent can place real orders against Binance in `live` mode. Before going live:

- Start in `paper` / simulation mode and confirm behavior.
- Fill in real keys only via environment variables — never commit `.env`.
- Review `security.*` limits in the config you run with.
- The `SecurityManager` runs a pre-send simulation and a circuit breaker by default; keep
  both enabled.

> Trading involves risk. This software is provided for educational and research purposes
> and is **not** financial advice.
