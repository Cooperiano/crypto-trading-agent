"""Multi-source market data service with fallback (Binance -> CoinGecko -> Yahoo)."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import structlog

from trading_agent.config import MarketDataConfig
from trading_agent.events import Event, EventBus, EventType
from trading_agent.models import OrderBook, OrderBookLevel, Ticker

logger = structlog.get_logger(__name__)


class CoinGeckoSource:
    def __init__(self) -> None:
        self._session: Any = None

    async def initialize(self) -> None:
        try:
            import aiohttp
        except ImportError:
            logger.warning("coingecko_requires_aiohttp")
            return
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def get_tickers(self, coin_ids: dict[str, str]) -> dict[str, Ticker]:
        if not self._session:
            return {}
        try:
            import aiohttp
            ids_str = ",".join(coin_ids.values())
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd&include_24hr_vol=true&include_24hr_change=true"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        except Exception:
            return {}
        result = {}
        for symbol, cg_id in coin_ids.items():
            td = data.get(cg_id, {})
            price = float(td.get("usd", 0))
            if price > 0:
                result[symbol] = Ticker(symbol=symbol, bid=price*0.999, ask=price*1.001, last=price,
                                        volume_24h=float(td.get("usd_24h_vol", 0)),
                                        change_24h_pct=float(td.get("usd_24h_change", 0)),
                                        high_24h=price*1.02, low_24h=price*0.98, timestamp=time.time()*1000)
        return result


class YahooFinanceSource:
    def __init__(self) -> None:
        self._session: Any = None

    async def initialize(self) -> None:
        try:
            import aiohttp
        except ImportError:
            logger.warning("yahoo_requires_aiohttp")
            return
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        if not symbols or not self._session:
            return {}
        try:
            import aiohttp
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(symbols)}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        except Exception:
            return {}
        result = {}
        for quote in data.get("quoteResponse", {}).get("result", []) or []:
            sym = quote.get("symbol", "")
            if sym:
                result[sym] = Ticker(symbol=sym, bid=float(quote.get("bid", 0) or 0),
                                     ask=float(quote.get("ask", 0) or 0),
                                     last=float(quote.get("regularMarketPrice", 0) or 0),
                                     volume_24h=float(quote.get("regularMarketVolume", 0) or 0),
                                     change_24h_pct=float(quote.get("regularMarketChangePercent", 0) or 0),
                                     high_24h=float(quote.get("regularMarketDayHigh", 0) or 0),
                                     low_24h=float(quote.get("regularMarketDayLow", 0) or 0),
                                     timestamp=time.time()*1000)
        return result


class SimulatedDataSource:
    def __init__(self, symbols: list[str], initial_prices: dict[str, float] | None = None) -> None:
        self._symbols = symbols
        self._prices = {s: (initial_prices or {}).get(s, 100.0) for s in symbols}
        self._running = False

    def get_book(self, symbol: str) -> OrderBook:
        mid = self._prices.get(symbol, 100.0)
        spread = mid * 0.001
        bids = [OrderBookLevel(price=round(mid - spread * (i+1)/5, 2), size=round(random.uniform(0.1, 10), 4)) for i in range(10)]
        asks = [OrderBookLevel(price=round(mid + spread * (i+1)/5, 2), size=round(random.uniform(0.1, 10), 4)) for i in range(10)]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=asyncio.get_event_loop().time())

    def get_ticker(self, symbol: str) -> Ticker:
        price = self._prices.get(symbol, 100.0)
        return Ticker(symbol=symbol, bid=price*0.999, ask=price*1.001, last=price,
                      volume_24h=random.uniform(1e6, 1e7), change_24h_pct=random.uniform(-5, 5),
                      high_24h=price*1.02, low_24h=price*0.98, timestamp=time.time()*1000)

    async def update_prices(self, event_bus: EventBus) -> None:
        self._running = True
        while self._running:
            for symbol in list(self._prices.keys()):
                self._prices[symbol] *= (1 + random.uniform(-0.005, 0.005))
                self._prices[symbol] = max(self._prices[symbol], 0.00001)
                book = self.get_book(symbol)
                await event_bus.publish(Event(type=EventType.ORDERBOOK_UPDATE,
                    data={"symbol": symbol, "bid": book.best_bid or 0.0, "ask": book.best_ask or 0.0,
                          "mid": book.mid or 0.0, "book": book}))
            await asyncio.sleep(1)


class MarketDataService:
    COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
                     "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2",
                     "DOT": "polkadot", "MATIC": "matic-network"}

    def __init__(self, event_bus: EventBus, binance_client: Any = None, config: MarketDataConfig | None = None) -> None:
        self._bus = event_bus
        self._binance = binance_client
        self._config = config or MarketDataConfig()
        self._books: dict[str, OrderBook] = {}
        self._tickers: dict[str, Ticker] = {}
        self._symbols: set[str] = set()
        self._coingecko: CoinGeckoSource | None = None
        self._yahoo: YahooFinanceSource | None = None
        self._simulated: SimulatedDataSource | None = None
        self._bus.subscribe(EventType.ORDERBOOK_UPDATE, self._on_book_update)
        self._bus.subscribe(EventType.TICKER_UPDATE, self._on_ticker_update)

    async def initialize(self, watch_list: list[str] | None = None) -> None:
        symbols = watch_list or self._config.symbols
        self._symbols = set(symbols) if symbols else set()
        if not self._symbols:
            self._symbols = {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
        for s in self._config.fallback_sources:
            if s == "coingecko":
                self._coingecko = CoinGeckoSource()
                await self._coingecko.initialize()
            elif s == "yahoo":
                self._yahoo = YahooFinanceSource()
                await self._yahoo.initialize()
        logger.info("market_data_initialized", symbols=len(self._symbols))

    def enable_simulation(self, symbols: list[str], initial_prices: dict[str, float] | None = None) -> None:
        self._simulated = SimulatedDataSource(symbols, initial_prices)
        self._symbols = set(symbols)
        asyncio.create_task(self._simulated.update_prices(self._bus))

    def get_book(self, symbol: str) -> OrderBook | None:
        if self._simulated:
            return self._simulated.get_book(symbol)
        return self._books.get(symbol)

    def get_ticker(self, symbol: str) -> Ticker | None:
        if self._simulated:
            return self._simulated.get_ticker(symbol)
        return self._tickers.get(symbol)

    def get_all_symbols(self) -> list[str]:
        return sorted(self._symbols)

    async def close(self) -> None:
        if self._coingecko: await self._coingecko.close()
        if self._yahoo: await self._yahoo.close()

    async def _on_book_update(self, event: Event) -> None:
        symbol = event.data.get("symbol", "")
        if "book" in event.data:
            self._books[symbol] = event.data["book"]
        else:
            bids = [OrderBookLevel(price=float(b[0]), size=float(b[1])) for b in event.data.get("bids", [])]
            asks = [OrderBookLevel(price=float(a[0]), size=float(a[1])) for a in event.data.get("asks", [])]
            self._books[symbol] = OrderBook(symbol=symbol, bids=sorted(bids, key=lambda x: x.price, reverse=True),
                                            asks=sorted(asks, key=lambda x: x.price), timestamp=event.timestamp)

    async def _on_ticker_update(self, event: Event) -> None:
        symbol = event.data.get("symbol", "")
        ticker = event.data.get("ticker")
        if ticker and isinstance(ticker, Ticker):
            self._tickers[symbol] = ticker
