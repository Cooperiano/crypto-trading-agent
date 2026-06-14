"""Async LLM client supporting multiple providers with market analysis prompts."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class ModelProvider(str, Enum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class MarketAnalysis(BaseModel):
    direction: str
    confidence: float
    target_price: float | None = None
    stop_loss_price: float | None = None
    time_horizon_hours: int = 4
    key_factors: list[str] = []
    risk_factors: list[str] = []
    reasoning: str = ""


class TradeSetup(BaseModel):
    symbol: str
    side: str
    entry_price: float
    target_price: float
    stop_loss: float
    position_size_pct: float
    confidence: float
    risk_reward_ratio: float
    reasoning: str


class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(self, prompt: str, system_prompt: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.7) -> str: ...
    @abstractmethod
    async def generate_structured(self, prompt: str, response_schema: type[BaseModel],
                                   system_prompt: str | None = None, max_tokens: int = 1024) -> BaseModel: ...
    @abstractmethod
    async def health_check(self) -> bool: ...


class OllamaClient(BaseLLMClient):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3",
                 max_tokens: int = 2048) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._session: Any = None

    async def initialize(self) -> None:
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(f"{self._base_url}/api/tags") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m["name"] for m in data.get("models", [])]
                    logger.info("ollama_connected", model=self._model, available=models)
        except Exception as e:
            logger.error("ollama_connection_failed", error=str(e))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def generate(self, prompt: str, system_prompt: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.7) -> str:
        payload = {"model": self._model, "prompt": prompt, "stream": False,
                   "options": {"num_predict": max_tokens, "temperature": temperature}}
        if system_prompt:
            payload["system"] = system_prompt
        async with self._session.post(f"{self._base_url}/api/generate", json=payload) as resp:
            data = await resp.json()
            return data.get("response", "")

    async def generate_structured(self, prompt: str, response_schema: type[BaseModel],
                                   system_prompt: str | None = None, max_tokens: int = 1024) -> BaseModel:
        schema_str = json.dumps(response_schema.model_json_schema(), indent=2)
        full_prompt = prompt + f"\n\nRespond with ONLY valid JSON matching this schema:\n{schema_str}"
        sys_prompt = (system_prompt or "") + "\nAlways respond with valid JSON only, no additional text."
        payload = {"model": self._model, "prompt": full_prompt, "system": sys_prompt,
                   "stream": False, "format": "json", "options": {"num_predict": max_tokens}}
        import aiohttp
        async with self._session.post(f"{self._base_url}/api/generate", json=payload,
                                       timeout=aiohttp.ClientTimeout(total=120)) as resp:
            data = await resp.json()
            result = json.loads(data.get("response", ""))
            return response_schema.model_validate(result)

    async def health_check(self) -> bool:
        try:
            async with self._session.get(f"{self._base_url}/api/tags") as resp:
                return resp.status == 200
        except Exception:
            return False


class OpenAIClient(BaseLLMClient):
    def __init__(self, base_url: str = "https://api.openai.com/v1", api_key: str = "",
                 model: str = "gpt-4", max_tokens: int = 2048) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._session: Any = None

    async def initialize(self) -> None:
        import aiohttp
        self._session = aiohttp.ClientSession(headers={
            "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"})

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def generate(self, prompt: str, system_prompt: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.7) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self._model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        async with self._session.post(f"{self._base_url}/chat/completions", json=payload) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    async def generate_structured(self, prompt: str, response_schema: type[BaseModel],
                                   system_prompt: str | None = None, max_tokens: int = 1024) -> BaseModel:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self._model, "messages": messages, "max_tokens": max_tokens,
                   "response_format": {"type": "json_object"}}
        async with self._session.post(f"{self._base_url}/chat/completions", json=payload) as resp:
            data = await resp.json()
            result = json.loads(data["choices"][0]["message"]["content"])
            return response_schema.model_validate(result)

    async def health_check(self) -> bool:
        try:
            async with self._session.get(f"{self._base_url}/models") as resp:
                return resp.status == 200
        except Exception:
            return False


class MultiModelClient:
    def __init__(self, clients: list[tuple[str, BaseLLMClient]] | None = None,
                 fallback_enabled: bool = True) -> None:
        self._clients = clients or []
        self._fallback_enabled = fallback_enabled

    def add_client(self, name: str, client: BaseLLMClient) -> None:
        self._clients.append((name, client))

    async def initialize_all(self) -> None:
        for name, client in self._clients:
            try:
                if hasattr(client, "initialize"):
                    await client.initialize()
                logger.info("llm_client_initialized", name=name)
            except Exception as e:
                logger.warning("llm_client_init_failed", name=name, error=str(e))

    async def close_all(self) -> None:
        for _, client in self._clients:
            try:
                if hasattr(client, "close"):
                    await client.close()
            except Exception:
                pass

    async def analyze_market(self, symbol: str, current_price: float, volume_24h: float = 0.0,
                              ohlcv_data: str | None = None, news: list[str] | None = None
                              ) -> tuple[MarketAnalysis, str]:
        prompt = _build_market_analysis_prompt(symbol, current_price, volume_24h, ohlcv_data, news)
        for name, client in self._clients:
            try:
                if await client.health_check():
                    analysis = await client.generate_structured(
                        prompt=prompt, response_schema=MarketAnalysis,
                        system_prompt=_MARKET_ANALYST_SYSTEM_PROMPT, max_tokens=2048)
                    return analysis, name
            except Exception as e:
                logger.warning("llm_analysis_failed", client=name, error=str(e))
                if not self._fallback_enabled:
                    raise
        raise RuntimeError("All LLM clients failed")

    async def generate_trade_setup(self, symbol: str, current_price: float,
                                    trend_info: str = "", risk_params: dict | None = None
                                    ) -> tuple[TradeSetup, str]:
        prompt = _build_trade_setup_prompt(symbol, current_price, trend_info, risk_params)
        for name, client in self._clients:
            try:
                if await client.health_check():
                    setup = await client.generate_structured(
                        prompt=prompt, response_schema=TradeSetup,
                        system_prompt=_TRADING_SYSTEM_PROMPT, max_tokens=2048)
                    return setup, name
            except Exception as e:
                logger.warning("llm_setup_failed", client=name, error=str(e))
                if not self._fallback_enabled:
                    raise
        raise RuntimeError("All LLM clients failed")


_MARKET_ANALYST_SYSTEM_PROMPT = """You are an expert cryptocurrency trader and quantitative analyst.
Analyze market conditions and provide directional trading insights.

Key principles:
- Trend is your friend: identify the dominant trend
- Support/Resistance: key price levels determine entry/exit
- Volume confirms: price moves on high volume are more significant
- Risk first: always consider downside before upside
- Multiple timeframes: confirm signals across timeframes

Always respond with valid JSON matching the required schema."""

_TRADING_SYSTEM_PROMPT = """You are an expert trading strategist.
Generate precise trade setups with entry, target, and stop prices.
Always respond with valid JSON matching the required schema."""


def _build_market_analysis_prompt(symbol: str, current_price: float, volume_24h: float = 0.0,
                                   ohlcv_data: str | None = None, news: list[str] | None = None) -> str:
    prompt = f"""## Market: {symbol}
## Current Price: ${current_price:.4f}
## 24h Volume: ${volume_24h:,.0f}
"""
    if ohlcv_data:
        prompt += f"\n## Recent Price Action\n{ohlcv_data}\n"
    if news:
        prompt += "\n## Recent News\n" + "\n".join(f"- {n}" for n in news[:5])
    prompt += """
## Analysis Task
1. Direction: long, short, or neutral
2. Confidence: 0-1 scale
3. Target price (if directional)
4. Stop loss price (if directional)
5. Time horizon in hours
6. Key factors
7. Risk factors
8. Detailed reasoning
Respond with valid JSON."""
    return prompt


def _build_trade_setup_prompt(symbol: str, current_price: float, trend_info: str = "",
                               risk_params: dict | None = None) -> str:
    prompt = f"""## Symbol: {symbol}
## Current Price: ${current_price:.4f}
## Trend Info: {trend_info or 'N/A'}
"""
    prompt += """Generate a trade setup with:
1. Side (BUY/SELL)
2. Entry price
3. Target price
4. Stop loss price
5. Position size as % of portfolio (0-1)
6. Confidence (0-1)
7. Risk/reward ratio
8. Reasoning
Respond with valid JSON."""
    return prompt
