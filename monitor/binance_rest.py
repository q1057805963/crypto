import asyncio
import json
import logging
import threading
import time
from typing import AsyncIterator
from urllib.parse import quote
from urllib.request import Request, urlopen


class BinanceFuturesTickerPoller:
    def __init__(
        self,
        symbols: list[str],
        poll_interval_seconds: float,
        per_symbol_delay_ms: int,
        oi_poll_interval_seconds: float,
        funding_poll_interval_seconds: float,
        microstructure_state=None,
    ) -> None:
        self._lock = threading.Lock()
        self.microstructure_state = microstructure_state
        self.symbols: set[str] = set()
        self.poll_interval_seconds = poll_interval_seconds
        self.per_symbol_delay_seconds = max(per_symbol_delay_ms, 0) / 1000
        self.url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        self.open_interest_url = "https://fapi.binance.com/fapi/v1/openInterest"
        self.premium_index_url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        self.oi_poll_interval_seconds = oi_poll_interval_seconds
        self.funding_poll_interval_seconds = funding_poll_interval_seconds
        self._last: dict[str, dict] = {}
        self._open_interest: dict[str, float] = {}
        self._funding_rate: dict[str, float] = {}
        self._last_oi_poll_at: dict[str, float] = {}
        self._last_funding_poll_at: dict[str, float] = {}
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            self.symbols = {symbol.upper() for symbol in symbols}

    def get_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self.symbols)

    async def listen(self) -> AsyncIterator[dict]:
        while True:
            started_at = time.time()
            symbols = self.get_symbols()

            for symbol in symbols:
                try:
                    ticker = await asyncio.to_thread(self._fetch_ticker, symbol)
                    event_time = time.time()
                    await self._refresh_slow_metrics(symbol, event_time)
                    trade = self._to_trade(ticker, event_time)
                    if trade:
                        yield trade
                except Exception as exc:
                    logging.warning("REST ticker poll failed for %s: %s", symbol, exc)

                if self.per_symbol_delay_seconds:
                    await asyncio.sleep(self.per_symbol_delay_seconds)

            elapsed = time.time() - started_at
            await asyncio.sleep(max(self.poll_interval_seconds - elapsed, 0))

    def _fetch_ticker(self, symbol: str) -> dict:
        request = Request(
            f"{self.url}?symbol={quote(symbol, safe='')}",
            headers={"User-Agent": "crypto-futures-monitor/0.1"},
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _refresh_slow_metrics(self, symbol: str, now: float) -> None:
        if now - self._last_oi_poll_at.get(symbol, 0) >= self.oi_poll_interval_seconds:
            try:
                value = await asyncio.to_thread(self._fetch_open_interest, symbol)
                self._open_interest[symbol] = value
                self._last_oi_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("Open interest poll failed for %s: %s", symbol, exc)

        if now - self._last_funding_poll_at.get(symbol, 0) >= self.funding_poll_interval_seconds:
            try:
                value = await asyncio.to_thread(self._fetch_funding_rate, symbol)
                self._funding_rate[symbol] = value
                self._last_funding_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("Funding rate poll failed for %s: %s", symbol, exc)

    def _fetch_open_interest(self, symbol: str) -> float:
        request = Request(
            f"{self.open_interest_url}?symbol={quote(symbol, safe='')}",
            headers={"User-Agent": "crypto-futures-monitor/0.1"},
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return float(payload.get("openInterest", 0))

    def _fetch_funding_rate(self, symbol: str) -> float:
        request = Request(
            f"{self.premium_index_url}?symbol={quote(symbol, safe='')}",
            headers={"User-Agent": "crypto-futures-monitor/0.1"},
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return float(payload.get("lastFundingRate", 0))

    def _to_trade(self, ticker: dict, event_time: float) -> dict | None:
        symbol = ticker.get("symbol", "").upper()
        if symbol not in self.symbols:
            return None

        price = float(ticker["lastPrice"])
        quote_volume_24h = float(ticker.get("quoteVolume", 0))

        previous = self._last.get(symbol)
        quote_quantity = 0.0
        if previous:
            quote_quantity = max(quote_volume_24h - previous["quote_volume_24h"], 0.0)

        last_quantity = quote_quantity / price if price > 0 else 0.0
        side = "buy"
        if previous and price < previous["price"]:
            side = "sell"

        self._last[symbol] = {
            "price": price,
            "event_time": event_time,
            "quote_volume_24h": quote_volume_24h,
        }

        trade = {
            "symbol": symbol,
            "event_time": event_time,
            "price": price,
            "quantity": last_quantity,
            "quote_quantity": quote_quantity,
            "side": side,
            "trade_id": int(event_time * 1000),
            "open_interest": self._open_interest.get(symbol, 0.0),
            "funding_rate": self._funding_rate.get(symbol, 0.0),
        }
        if self.microstructure_state:
            trade.update(self.microstructure_state.snapshot(symbol, event_time))
        return trade
