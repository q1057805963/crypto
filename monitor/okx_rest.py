import asyncio
import json
import logging
import threading
import time
from typing import AsyncIterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class OkxSwapTickerPoller:
    def __init__(
        self,
        symbols: list[str],
        poll_interval_seconds: float,
        per_symbol_delay_ms: int,
        oi_poll_interval_seconds: float,
        funding_poll_interval_seconds: float,
        depth_poll_interval_seconds: float,
        liquidation_poll_interval_seconds: float,
        microstructure_state=None,
    ) -> None:
        self._lock = threading.Lock()
        self.microstructure_state = microstructure_state
        self.symbols: set[str] = set()
        self.poll_interval_seconds = poll_interval_seconds
        self.per_symbol_delay_seconds = max(per_symbol_delay_ms, 0) / 1000
        self.oi_poll_interval_seconds = oi_poll_interval_seconds
        self.funding_poll_interval_seconds = funding_poll_interval_seconds
        self.depth_poll_interval_seconds = depth_poll_interval_seconds
        self.liquidation_poll_interval_seconds = liquidation_poll_interval_seconds
        self.base_url = "https://www.okx.com"
        self._last: dict[str, dict] = {}
        self._open_interest: dict[str, float] = {}
        self._funding_rate: dict[str, float] = {}
        self._instrument_specs: dict[str, dict] = {}
        self._seen_liquidations: dict[str, dict[str, float]] = {}
        self._last_oi_poll_at: dict[str, float] = {}
        self._last_funding_poll_at: dict[str, float] = {}
        self._last_depth_poll_at: dict[str, float] = {}
        self._last_liquidation_poll_at: dict[str, float] = {}
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
                    event_time = self._event_time(ticker)
                    await self._refresh_slow_metrics(symbol, event_time)
                    trade = self._to_trade(symbol, ticker, event_time)
                    if trade:
                        yield trade
                except Exception as exc:
                    logging.warning("OKX ticker poll failed for %s: %s", symbol, exc)

                if self.per_symbol_delay_seconds:
                    await asyncio.sleep(self.per_symbol_delay_seconds)

            elapsed = time.time() - started_at
            await asyncio.sleep(max(self.poll_interval_seconds - elapsed, 0))

    async def _refresh_slow_metrics(self, symbol: str, now: float) -> None:
        if self.microstructure_state and now - self._last_depth_poll_at.get(symbol, 0) >= self.depth_poll_interval_seconds:
            try:
                event_time, bids, asks = await asyncio.to_thread(self._fetch_depth, symbol)
                self.microstructure_state.record_depth(symbol, event_time, bids, asks)
                self._last_depth_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("OKX depth poll failed for %s: %s", symbol, exc)

        if (
            self.microstructure_state
            and now - self._last_liquidation_poll_at.get(symbol, 0) >= self.liquidation_poll_interval_seconds
        ):
            try:
                liquidations = await asyncio.to_thread(self._fetch_liquidations, symbol, now)
                self.microstructure_state.mark_liquidation_feed(symbol, now)
                self._record_liquidations(symbol, liquidations, now)
                self._last_liquidation_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("OKX liquidation poll failed for %s: %s", symbol, exc)

        if now - self._last_oi_poll_at.get(symbol, 0) >= self.oi_poll_interval_seconds:
            try:
                value = await asyncio.to_thread(self._fetch_open_interest, symbol)
                self._open_interest[symbol] = value
                self._last_oi_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("OKX open interest poll failed for %s: %s", symbol, exc)

        if now - self._last_funding_poll_at.get(symbol, 0) >= self.funding_poll_interval_seconds:
            try:
                value = await asyncio.to_thread(self._fetch_funding_rate, symbol)
                self._funding_rate[symbol] = value
                self._last_funding_poll_at[symbol] = now
            except Exception as exc:
                logging.warning("OKX funding rate poll failed for %s: %s", symbol, exc)

    def _fetch_ticker(self, symbol: str) -> dict:
        payload = self._get_json(
            "/api/v5/market/ticker",
            {"instId": self._inst_id(symbol)},
        )
        data = payload.get("data") or []
        if not data:
            raise ValueError(f"empty OKX ticker response: {payload.get('msg', '')}")
        return data[0]

    def _fetch_open_interest(self, symbol: str) -> float:
        payload = self._get_json(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": self._inst_id(symbol)},
        )
        data = payload.get("data") or []
        if not data:
            return 0.0
        item = data[0]
        if item.get("oiUsd"):
            return float(item["oiUsd"])
        price = self._last.get(symbol, {}).get("price") or 0
        if item.get("oiCcy") and price:
            return float(item["oiCcy"]) * float(price)
        return float(item.get("oi") or 0)

    def _fetch_funding_rate(self, symbol: str) -> float:
        payload = self._get_json(
            "/api/v5/public/funding-rate",
            {"instId": self._inst_id(symbol)},
        )
        data = payload.get("data") or []
        if not data:
            return 0.0
        return float(data[0].get("fundingRate") or 0)

    def _fetch_depth(self, symbol: str) -> tuple[float, list[list[float]], list[list[float]]]:
        payload = self._get_json(
            "/api/v5/market/books",
            {"instId": self._inst_id(symbol), "sz": "10"},
        )
        data = payload.get("data") or []
        if not data:
            raise ValueError("empty OKX depth response")
        book = data[0]
        event_time = float(book.get("ts") or int(time.time() * 1000)) / 1000
        bids = self._convert_depth_levels(symbol, book.get("bids") or [])
        asks = self._convert_depth_levels(symbol, book.get("asks") or [])
        return event_time, bids, asks

    def _fetch_liquidations(self, symbol: str, now: float) -> list[dict]:
        payload = self._get_json(
            "/api/v5/public/liquidation-orders",
            {
                "instType": "SWAP",
                "instFamily": self._inst_family(symbol),
                "state": "filled",
            },
        )
        inst_id = self._inst_id(symbol)
        cutoff = now - 90
        liquidations = []
        for group in payload.get("data") or []:
            group_inst_id = str(group.get("instId") or "")
            if group_inst_id and group_inst_id != inst_id:
                continue
            for item in group.get("details") or []:
                event_time = float(item.get("ts") or item.get("time") or 0) / 1000
                if event_time < cutoff:
                    continue
                price = float(item.get("bkPx") or 0)
                contracts = float(item.get("sz") or 0)
                if price <= 0 or contracts <= 0:
                    continue
                side = str(item.get("side") or "sell").upper()
                base_quantity = self._contracts_to_base_quantity(symbol, price, contracts)
                liquidation_id = "|".join(
                    [
                        str(item.get("ts") or item.get("time") or ""),
                        str(item.get("bkPx") or ""),
                        str(item.get("sz") or ""),
                        str(item.get("side") or ""),
                        str(item.get("posSide") or ""),
                    ]
                )
                liquidations.append(
                    {
                        "id": liquidation_id,
                        "event_time": event_time,
                        "side": side,
                        "price": price,
                        "quantity": base_quantity,
                    }
                )
        return liquidations

    def _record_liquidations(self, symbol: str, liquidations: list[dict], now: float) -> None:
        if not self.microstructure_state:
            return

        seen = self._seen_liquidations.setdefault(symbol, {})
        for key, event_time in list(seen.items()):
            if event_time < now - 180:
                seen.pop(key, None)

        for item in sorted(liquidations, key=lambda value: value["event_time"]):
            liquidation_id = str(item["id"])
            if liquidation_id in seen:
                continue
            seen[liquidation_id] = float(item["event_time"])
            self.microstructure_state.record_liquidation(
                symbol=symbol,
                event_time=float(item["event_time"]),
                side=str(item["side"]),
                price=float(item["price"]),
                quantity=float(item["quantity"]),
            )

    def _convert_depth_levels(self, symbol: str, levels: list[list[str]]) -> list[list[float]]:
        converted = []
        for level in levels:
            if len(level) < 2:
                continue
            price = float(level[0])
            contracts = float(level[1])
            base_quantity = self._contracts_to_base_quantity(symbol, price, contracts)
            converted.append([price, base_quantity])
        return converted

    def _contracts_to_base_quantity(self, symbol: str, price: float, contracts: float) -> float:
        spec = self._instrument_spec(symbol)
        ct_val = float(spec.get("ctVal") or 1)
        ct_val_ccy = str(spec.get("ctValCcy") or "").upper()
        settle_ccy = str(spec.get("settleCcy") or "").upper()
        if ct_val_ccy in {"USD", "USDT", settle_ccy} and price > 0:
            return contracts * ct_val / price
        return contracts * ct_val

    def _instrument_spec(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in self._instrument_specs:
            payload = self._get_json(
                "/api/v5/public/instruments",
                {"instType": "SWAP", "instId": self._inst_id(symbol)},
            )
            data = payload.get("data") or []
            self._instrument_specs[symbol] = data[0] if data else {}
        return self._instrument_specs[symbol]

    def _to_trade(self, symbol: str, ticker: dict, event_time: float) -> dict | None:
        symbol = symbol.upper()
        if symbol not in self.symbols:
            return None

        price = float(ticker["last"])
        quote_volume_24h = self._quote_volume_24h(ticker, price)
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

    @staticmethod
    def _quote_volume_24h(ticker: dict, price: float) -> float:
        quote_volume = ticker.get("volCcyQuote24h")
        if quote_volume not in (None, ""):
            return float(quote_volume)
        base_volume = float(ticker.get("volCcy24h") or 0)
        if base_volume and price:
            return base_volume * price
        return float(ticker.get("vol24h") or 0)

    @staticmethod
    def _event_time(ticker: dict) -> float:
        timestamp = ticker.get("ts")
        if timestamp:
            return float(timestamp) / 1000
        return time.time()

    @staticmethod
    def _inst_id(symbol: str) -> str:
        symbol = symbol.upper()
        if "-" in symbol:
            return symbol
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{base}-USDT-SWAP"

    @staticmethod
    def _inst_family(symbol: str) -> str:
        inst_id = OkxSwapTickerPoller._inst_id(symbol)
        parts = inst_id.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return inst_id

    def _get_json(self, path: str, params: dict[str, str]) -> dict:
        query = urlencode(params)
        request = Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "crypto-futures-monitor/0.1",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("code")) != "0":
            raise ValueError(payload.get("msg") or payload)
        return payload
