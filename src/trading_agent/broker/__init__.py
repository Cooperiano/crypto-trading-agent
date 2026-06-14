"""Binance broker adapter — REST client + WebSocket for real-time data."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import ccxt.async_support as ccxt
import structlog

from trading_agent.models import OrderBook, OrderBookLevel, Ticker, Trade, Side, OrderType, TimeInForce

logger = structlog.get_logger(__name__)


class BinanceClient:
    """Async Binance client wrapping ccxt for REST operations."""

    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True) -> None:
        exchange_config: dict[str, Any] = {
            "apiKey": api_key, "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "fetchCurrencies": not testnet,
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            },
        }
        if testnet:
            exchange_config["urls"] = {
                "api": {
                    "public": "https://testnet.binance.vision/api",
                    "private": "https://testnet.binance.vision/api",
                }
            }
        self._exchange: ccxt.binance = ccxt.binance(exchange_config)
        if testnet:
            self._exchange.set_sandbox_mode(True)
        self._testnet = testnet

    async def initialize(self) -> None:
        try:
            await self._exchange.load_markets()
        except Exception as e:
            logger.warning("load_markets_failed", error=str(e)[:80])
            self._exchange.markets = {}
        logger.info("binance_connected", testnet=self._testnet, markets=len(self._exchange.markets))

    async def close(self) -> None:
        await self._exchange.close()

    async def fetch_ticker(self, symbol: str) -> Ticker:
        t = await self._exchange.fetch_ticker(symbol)
        return Ticker(symbol=symbol, bid=t.get("bid", 0.0) or 0.0, ask=t.get("ask", 0.0) or 0.0,
                      last=t.get("last", 0.0) or 0.0, volume_24h=t.get("baseVolume", 0.0) or 0.0,
                      change_24h_pct=t.get("percentage", 0.0) or 0.0, high_24h=t.get("high", 0.0) or 0.0,
                      low_24h=t.get("low", 0.0) or 0.0, timestamp=t.get("timestamp", 0.0) or 0.0)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        raw = await self._exchange.fetch_order_book(symbol, limit)
        return OrderBook(symbol=symbol, bids=[OrderBookLevel(price=b[0], size=b[1]) for b in (raw.get("bids", []) or [])],
                         asks=[OrderBookLevel(price=a[0], size=a[1]) for a in (raw.get("asks", []) or [])],
                         timestamp=raw.get("timestamp", 0.0) or 0.0)

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Ticker]:
        raw = await self._exchange.fetch_tickers(symbols) if symbols else {}
        result = {}
        for sym, t in (raw or {}).items():
            result[sym] = Ticker(symbol=sym, bid=t.get("bid", 0.0) or 0.0, ask=t.get("ask", 0.0) or 0.0,
                                  last=t.get("last", 0.0) or 0.0, volume_24h=t.get("baseVolume", 0.0) or 0.0,
                                  change_24h_pct=t.get("percentage", 0.0) or 0.0, high_24h=t.get("high", 0.0) or 0.0,
                                  low_24h=t.get("low", 0.0) or 0.0, timestamp=t.get("timestamp", 0.0) or 0.0)
        return result

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[list[float]]:
        return await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit) or []

    async def get_balance(self, currency: str = "USDT") -> float:
        balance = await self._exchange.fetch_balance()
        return float((balance.get("total", {}) or {}).get(currency, 0.0) or 0.0)

    async def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        return await self._exchange.create_order(symbol=symbol, type="limit", side=side.lower(), amount=amount, price=price) or {}

    async def place_market_order(self, symbol: str, side: str, amount: float) -> dict:
        return await self._exchange.create_order(symbol=symbol, type="market", side=side.lower(), amount=amount) or {}

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self._exchange.cancel_order(order_id, symbol)
            return True
        except Exception:
            return False

    async def cancel_all(self) -> int:
        orders = await self._exchange.fetch_open_orders() or []
        cancelled = 0
        for o in orders:
            try:
                await self._exchange.cancel_order(o["id"], o["symbol"])
                cancelled += 1
            except Exception:
                pass
        return cancelled


class BinanceWebSocket:
    """Async Binance WebSocket for real-time market data."""

    def __init__(self, testnet: bool = False) -> None:
        self._ws: Any = None
        self._running = False
        self._callbacks: dict[str, list] = {}
        self._testnet = testnet

    async def connect(self, streams: list[str]) -> None:
        self._running = True
        import websockets
        host = "stream.testnet.binance.vision" if self._testnet else "stream.binance.com"
        ws_url = f"wss://{host}:9443/ws"
        retry_count = 0
        while self._running:
            try:
                async with websockets.connect(f"{ws_url}/{'/'.join(streams)}") as ws:
                    self._ws = ws
                    retry_count = 0
                    logger.info("binance_ws_connected", streams=len(streams))
                    async for message in ws:
                        if not self._running:
                            break
                        data = json.loads(message)
                        for cb in self._callbacks.get(data.get("e", ""), []):
                            await cb(data)
            except Exception as e:
                retry_count += 1
                logger.warning("ws_disconnected", error=str(e), retry_in=min(retry_count * 2, 30))
                await asyncio.sleep(min(retry_count * 2, 30))

    def on(self, stream_type: str, callback):
        self._callbacks.setdefault(stream_type, []).append(callback)

    async def stop(self) -> None:
        self._running = False

    @staticmethod
    def build_depth_stream(symbols: list[str]) -> list[str]:
        return [f"{s.lower()}@depth20@100ms" for s in symbols]

    @staticmethod
    def build_ticker_stream(symbols: list[str]) -> list[str]:
        return [f"{s.lower()}@ticker" for s in symbols]
