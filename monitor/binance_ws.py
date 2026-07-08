import asyncio
import json
import logging
from typing import AsyncIterator

import websockets

from monitor.binance_rest import BinanceFuturesTickerPoller


class BinanceFuturesAggTradeStream:
    def __init__(self, symbols: list[str], microstructure_state=None) -> None:
        self.microstructure_state = microstructure_state
        self._state: dict[str, dict] = {}
        self._slow_tasks: dict[str, asyncio.Task] = {}
        self._last_oi_poll_at: dict[str, float] = {}
        self._last_funding_poll_at: dict[str, float] = {}
        self.oi_poll_interval_seconds = 30.0
        self.funding_poll_interval_seconds = 60.0
        self._rest_helper = BinanceFuturesTickerPoller(
            symbols,
            poll_interval_seconds=5,
            per_symbol_delay_ms=0,
            oi_poll_interval_seconds=self.oi_poll_interval_seconds,
            funding_poll_interval_seconds=self.funding_poll_interval_seconds,
        )
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = [symbol.upper() for symbol in symbols]
        # 中文合约名（如 币安人生USDT）无法放进 URL 的 streams 参数，
        # 统一改为连接后发 SUBSCRIBE 消息订阅
        self._streams = [f"{symbol.lower()}@aggTrade" for symbol in self.symbols]
        self.url = "wss://fstream.binance.com/stream"
        self._rest_helper.set_symbols(symbols)

    async def listen(self) -> AsyncIterator[dict]:
        backoff_seconds = 1
        while True:
            try:
                logging.info("Connecting to Binance Futures WebSocket")
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {"method": "SUBSCRIBE", "params": self._streams, "id": 1},
                            ensure_ascii=False,
                        )
                    )
                    backoff_seconds = 1
                    async for raw_message in websocket:
                        trade = self._parse(raw_message)
                        if trade:
                            yield trade
            except Exception as exc:
                logging.warning(
                    "WebSocket disconnected: %s. Reconnecting in %ss",
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    def _parse(self, raw_message: str) -> dict | None:
        payload = json.loads(raw_message)
        data = payload.get("data", payload)
        if data.get("e") != "aggTrade":
            return None

        price = float(data["p"])
        quantity = float(data["q"])
        is_buyer_maker = bool(data["m"])
        side = "sell" if is_buyer_maker else "buy"
        symbol = data["s"]

        state = self._state.get(symbol, {})
        trade = {
            "symbol": symbol,
            "event_time": data["E"] / 1000,
            "price": price,
            "quantity": quantity,
            "quote_quantity": price * quantity,
            "side": side,
            "trade_id": data["a"],
            "open_interest": state.get("open_interest", 0.0),
            "funding_rate": state.get("funding_rate", 0.0),
        }
        self._maybe_poll_slow_metrics(symbol, trade["event_time"])
        if self.microstructure_state:
            trade.update(self.microstructure_state.snapshot(symbol, trade["event_time"]))
        return trade

    def _maybe_poll_slow_metrics(self, symbol: str, now: float) -> None:
        poll_oi = now - self._last_oi_poll_at.get(symbol, 0) >= self.oi_poll_interval_seconds
        poll_funding = (
            now - self._last_funding_poll_at.get(symbol, 0)
            >= self.funding_poll_interval_seconds
        )
        if not poll_oi and not poll_funding:
            return
        existing = self._slow_tasks.get(symbol)
        if existing and not existing.done():
            return
        # 失败同样计入节流窗口，避免网络异常时每笔成交都触发重试
        if poll_oi:
            self._last_oi_poll_at[symbol] = now
        if poll_funding:
            self._last_funding_poll_at[symbol] = now
        self._slow_tasks[symbol] = asyncio.create_task(
            self._refresh_slow_metrics(symbol, poll_oi, poll_funding)
        )

    async def _refresh_slow_metrics(self, symbol: str, poll_oi: bool, poll_funding: bool) -> None:
        if poll_oi:
            try:
                value = await asyncio.to_thread(self._rest_helper._fetch_open_interest, symbol)
                self._state.setdefault(symbol, {})["open_interest"] = value
            except Exception as exc:
                logging.debug("Binance open interest poll failed for %s: %s", symbol, exc)
        if poll_funding:
            try:
                value = await asyncio.to_thread(self._rest_helper._fetch_funding_rate, symbol)
                self._state.setdefault(symbol, {})["funding_rate"] = value
            except Exception as exc:
                logging.debug("Binance funding rate poll failed for %s: %s", symbol, exc)
