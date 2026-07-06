import asyncio
import json
import logging
import threading
import time
from typing import AsyncIterator

import websockets

from monitor.okx_rest import OkxSwapTickerPoller


CHANNEL_LABELS = {
    "trades": "成交",
    "tickers": "Ticker",
    "books5": "盘口",
    "mark-price": "标记价",
    "funding-rate": "资金费率",
    "open-interest": "持仓量",
    "liquidations": "强平补偿",
}


CHANNEL_STALE_SECONDS = {
    "trades": 20,
    "tickers": 20,
    "books5": 20,
    "mark-price": 60,
    "funding-rate": 180,
    "open-interest": 60,
    "liquidations": 45,
}


class OkxSwapWebSocketStream:
    def __init__(
        self,
        symbols: list[str],
        liquidation_poll_interval_seconds: float,
        microstructure_state=None,
    ) -> None:
        self._lock = threading.Lock()
        self.microstructure_state = microstructure_state
        self.url = "wss://ws.okx.com:8443/ws/v5/public"
        self.symbols: set[str] = set()
        self._state: dict[str, dict] = {}
        self._last_channel_at: dict[str, dict[str, float]] = {}
        self._last_liquidation_poll_at: dict[str, float] = {}
        self._liquidation_tasks: dict[str, asyncio.Task] = {}
        self.liquidation_poll_interval_seconds = liquidation_poll_interval_seconds
        self._rest_helper = OkxSwapTickerPoller(
            symbols,
            poll_interval_seconds=5,
            per_symbol_delay_ms=0,
            oi_poll_interval_seconds=30,
            funding_poll_interval_seconds=60,
            depth_poll_interval_seconds=5,
            liquidation_poll_interval_seconds=liquidation_poll_interval_seconds,
            microstructure_state=microstructure_state,
        )
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            self.symbols = {symbol.upper() for symbol in symbols}
            for symbol in self.symbols:
                self._state.setdefault(symbol, {})
                self._last_channel_at.setdefault(symbol, {})
        self._rest_helper.set_symbols(symbols)

    def get_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self.symbols)

    async def listen(self) -> AsyncIterator[dict]:
        backoff_seconds = 1
        while True:
            try:
                logging.info("Connecting to OKX WebSocket")
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    await websocket.send(json.dumps({"op": "subscribe", "args": self._subscribe_args()}))
                    backoff_seconds = 1
                    async for raw_message in websocket:
                        trade = await self._handle_message(raw_message)
                        if trade:
                            yield trade
            except Exception as exc:
                logging.warning(
                    "OKX WebSocket disconnected: %s. Reconnecting in %ss",
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    def _subscribe_args(self) -> list[dict]:
        args = []
        for symbol in self.get_symbols():
            inst_id = self._rest_helper._inst_id(symbol)
            args.extend(
                [
                    {"channel": "trades", "instId": inst_id},
                    {"channel": "tickers", "instId": inst_id},
                    {"channel": "books5", "instId": inst_id},
                    {"channel": "mark-price", "instId": inst_id},
                    {"channel": "funding-rate", "instId": inst_id},
                    {"channel": "open-interest", "instId": inst_id},
                ]
            )
        return args

    async def _handle_message(self, raw_message: str) -> dict | None:
        payload = json.loads(raw_message)
        event = payload.get("event")
        if event:
            if event == "error":
                logging.warning("OKX WebSocket subscription error: %s", payload)
            return None

        arg = payload.get("arg") or {}
        channel = str(arg.get("channel") or "")
        inst_id = str(arg.get("instId") or "")
        symbol = self._symbol_from_inst_id(inst_id)
        if not symbol or symbol not in self.symbols:
            return None

        data = payload.get("data") or []
        if data and channel:
            self._record_channel(symbol, channel)
        if channel == "trades":
            return await self._handle_trades(symbol, data)
        if channel == "tickers":
            self._handle_ticker(symbol, data)
        elif channel == "books5":
            self._handle_books(symbol, data)
        elif channel == "mark-price":
            self._handle_mark_price(symbol, data)
        elif channel == "funding-rate":
            self._handle_funding_rate(symbol, data)
        elif channel == "open-interest":
            self._handle_open_interest(symbol, data)
        return None

    async def _handle_trades(self, symbol: str, data: list[dict]) -> dict | None:
        if not data:
            return None
        latest_trade = None
        for item in data:
            price = float(item.get("px") or 0)
            contracts = float(item.get("sz") or 0)
            if price <= 0 or contracts <= 0:
                continue
            event_time = float(item.get("ts") or int(time.time() * 1000)) / 1000
            quantity = self._rest_helper._contracts_to_base_quantity(symbol, price, contracts)
            side = str(item.get("side") or "buy").lower()
            trade_id = item.get("tradeId") or item.get("seqId") or int(event_time * 1000)
            self._state.setdefault(symbol, {})["price"] = price
            mark_price = float(self._state.get(symbol, {}).get("mark_price") or 0)
            latest_trade = {
                "symbol": symbol,
                "event_time": event_time,
                "price": price,
                "quantity": quantity,
                "quote_quantity": price * quantity,
                "side": "sell" if side == "sell" else "buy",
                "trade_id": self._trade_id(trade_id),
                "open_interest": self._state.get(symbol, {}).get("open_interest", 0.0),
                "funding_rate": self._state.get(symbol, {}).get("funding_rate", 0.0),
                "mark_price": mark_price,
            }
            self._maybe_poll_liquidations(symbol, event_time)
            if self.microstructure_state:
                latest_trade.update(self.microstructure_state.snapshot(symbol, event_time))
        return latest_trade

    def _handle_ticker(self, symbol: str, data: list[dict]) -> None:
        if not data:
            return
        item = data[-1]
        price = float(item.get("last") or 0)
        if price > 0:
            self._state.setdefault(symbol, {})["price"] = price

    def _handle_books(self, symbol: str, data: list[dict]) -> None:
        if not data or not self.microstructure_state:
            return
        item = data[-1]
        event_time = float(item.get("ts") or int(time.time() * 1000)) / 1000
        bids = self._convert_depth_levels(symbol, item.get("bids") or [])
        asks = self._convert_depth_levels(symbol, item.get("asks") or [])
        self.microstructure_state.record_depth(symbol, event_time, bids, asks)

    def _handle_mark_price(self, symbol: str, data: list[dict]) -> None:
        if not data:
            return
        mark_price = float(data[-1].get("markPx") or 0)
        if mark_price > 0:
            self._state.setdefault(symbol, {})["mark_price"] = mark_price

    def _handle_funding_rate(self, symbol: str, data: list[dict]) -> None:
        if not data:
            return
        self._state.setdefault(symbol, {})["funding_rate"] = float(data[-1].get("fundingRate") or 0)

    def _handle_open_interest(self, symbol: str, data: list[dict]) -> None:
        if not data:
            return
        item = data[-1]
        price = float(self._state.get(symbol, {}).get("price") or 0)
        if item.get("oiUsd") not in (None, ""):
            value = float(item.get("oiUsd") or 0)
        elif item.get("oiCcy") not in (None, "") and price > 0:
            value = float(item.get("oiCcy") or 0) * price
        else:
            contracts = float(item.get("oi") or 0)
            quantity = self._rest_helper._contracts_to_base_quantity(symbol, price, contracts)
            value = quantity * price if price > 0 else contracts
        self._state.setdefault(symbol, {})["open_interest"] = value

    def _maybe_poll_liquidations(self, symbol: str, now: float) -> None:
        if not self.microstructure_state:
            return
        if now - self._last_liquidation_poll_at.get(symbol, 0) < self.liquidation_poll_interval_seconds:
            return
        existing = self._liquidation_tasks.get(symbol)
        if existing and not existing.done():
            return
        # 失败同样计入节流窗口，避免网络异常时每笔成交都触发重试
        self._last_liquidation_poll_at[symbol] = now
        self._liquidation_tasks[symbol] = asyncio.create_task(
            self._refresh_liquidations(symbol, now)
        )

    async def _refresh_liquidations(self, symbol: str, now: float) -> None:
        try:
            liquidations = await asyncio.to_thread(self._rest_helper._fetch_liquidations, symbol, now)
            self.microstructure_state.mark_liquidation_feed(symbol, now)
            self._rest_helper._record_liquidations(symbol, liquidations, now)
            self._record_channel(symbol, "liquidations", now)
        except Exception as exc:
            logging.warning("OKX liquidation poll failed for %s: %s", symbol, exc)

    def health_summary(self, now: float | None = None) -> dict:
        now = float(now or time.time())
        symbols = self.get_symbols()
        with self._lock:
            last_channel_at = {
                symbol: dict(self._last_channel_at.get(symbol, {}))
                for symbol in symbols
            }
        channels = []
        for key, label in CHANNEL_LABELS.items():
            latest_at = max(
                (
                    last_channel_at.get(symbol, {}).get(key, 0.0)
                    for symbol in symbols
                ),
                default=0.0,
            )
            age_seconds = now - latest_at if latest_at else None
            stale_after = CHANNEL_STALE_SECONDS.get(key, 60)
            if age_seconds is None:
                status = "unavailable"
            elif age_seconds <= stale_after:
                status = "active"
            else:
                status = "stale"
            channels.append(
                {
                    "key": key,
                    "label": label,
                    "status": status,
                    "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                    "stale_after_seconds": stale_after,
                }
            )
        active_count = sum(1 for item in channels if item["status"] == "active")
        return {
            "status": "active" if active_count >= 3 else "degraded",
            "active_count": active_count,
            "total_count": len(channels),
            "channels": channels,
        }

    def _record_channel(self, symbol: str, channel: str, event_time: float | None = None) -> None:
        with self._lock:
            self._last_channel_at.setdefault(symbol.upper(), {})[channel] = float(event_time or time.time())

    def _convert_depth_levels(self, symbol: str, levels: list[list[str]]) -> list[list[float]]:
        converted = []
        for level in levels:
            if len(level) < 2:
                continue
            price = float(level[0])
            contracts = float(level[1])
            converted.append(
                [price, self._rest_helper._contracts_to_base_quantity(symbol, price, contracts)]
            )
        return converted

    @staticmethod
    def _symbol_from_inst_id(inst_id: str) -> str:
        parts = inst_id.upper().split("-")
        if len(parts) >= 2:
            return f"{parts[0]}{parts[1]}"
        return inst_id.upper()

    @staticmethod
    def _trade_id(raw_value) -> int:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return abs(hash(str(raw_value))) % 10_000_000_000
