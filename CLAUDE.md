# CLAUDE.md — trading-agent

Guidance for AI agents (Claude Code et al.) working in this repo.

## What this is

An async, event-driven cryptocurrency trading agent for Binance. A single `EventBus`
connects market data → strategies → execution, with `RiskManager` and `SecurityManager`
as hard guardrails. Paper/simulation by default; real trading only when explicitly
configured.

Python ≥ 3.11. Managed with `pyproject.toml` + a local `.venv`. Always use the project
venv binary (`.venv/bin/python`, `.venv/bin/trading-agent`, `.venv/bin/pytest`, …).

## Environment

- Interpreter / deps: `.venv/` (editable install of `trading_agent`).
- Run: `.venv/bin/trading-agent --config config/<profile>.yaml`.
- Default config (`config/default.yaml`) runs the price simulator — no API keys, no network
  to Binance. Use it for any smoke test.
- Secrets come from env vars (prefix `TRADE_`, nested with `__`) — see `.env.example`.
  **Never** commit real keys; `.env` is gitignored.

## Architecture (read before changing cross-module behavior)

- **Entry point**: `src/trading_agent/__main__.py` — `run_agent(config)` wires every
  component, starts the event bus + heartbeat + dashboard, and owns graceful shutdown.
- **Events**: `events.py` defines `EventBus`, `Event` (frozen dataclass), and `EventType`.
  Modules communicate **only** by publishing/subscribing on the bus — do not call into other
  modules directly.
- **Config**: `config.py` — Pydantic `BaseSettings` (`AppConfig`) with YAML loader
  (`load_config`). YAML keys override via env (`TRADE_<SECTION>__<KEY>`).
- **Strategies are plugins**: `strategies/base.py` (`BaseStrategy`) + `strategies/registry.py`
  (`StrategyRegistry.discover()`). Discovery scans `strategies/` for `BaseStrategy`
  subclasses and auto-aliases them to kebab-case. To add a strategy: subclass `BaseStrategy`,
  implement `start`/`stop`/`on_event`, emit `Signal`s via `self.emit_signal`, enable under
  `strategies.<name>:` in config.
- **Models**: `models.py` — `Ticker`, `OrderBookLevel`, `Signal`, `Trade`, `Position`, etc.
- **Guardrails**: `risk/` (sizing, daily-loss limit, stop loss) and `security/` (per-tx cap,
  daily spend cap, circuit breaker, injection guard, pre-send simulation, audit log) are
  separate and both run on every order path. Keep them enabled in any non-paper config.

## Conventions

- Type-annotate everything; `mypy --ignore-missing-imports src/trading_agent/` should stay
  green on files you touch.
- `ruff` is the linter/formatter (line length 120). Run `ruff check src/`.
- Logging via `structlog` (`structlog.get_logger(__name__)`), not `print`. Keep the
  `snake_case` event key style you see in existing log calls (`logger.info("agent_running", …)`).
- Prefer immutable models/dataclasses (existing code uses `@dataclass(frozen=True)` for
  `Event`, Pydantic `BaseModel` for config). Don't mutate shared event data in handlers.
- Async everywhere in the hot path — handlers are coroutines subscribed via
  `event_bus.subscribe`. Do not block the event loop.

## Testing

- Framework: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), tests in `tests/`.
- The simulator + paper config let tests run with no network/keys. Prefer constructing
  components (EventBus, a strategy, fake/mocked clients) and asserting on emitted events
  rather than hitting Binance.

## Workflow guardrails

- Smoke-test before claiming a change works:
  `.venv/bin/trading-agent --config config/default.yaml` should boot to `agent_running` and
  shut down cleanly on SIGTERM/SIGINT.
- For changes to order execution, risk, or security paths: run in paper/simulation and
  confirm no real orders can be triggered. Treat the `SecurityManager` circuit breaker and
  pre-send simulation as load-bearing.
- Known nit (non-blocking): shutdown currently logs "Unclosed client session" for two
  aiohttp sessions. Safe to fix in a cleanup pass, but it does not affect the running agent.
