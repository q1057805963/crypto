import asyncio
import json
import logging
from typing import AsyncIterator

import websockets


class BinanceFuturesAggTradeStream:
    def __init__(self, symbols: list[str]) -> None:
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = [symbol.upper() for symbol in symbols]
        streams = "/".join(f"{symbol.lower()}@aggTrade" for symbol in self.symbols)
        self.url = f"wss://fstream.binance.com/stream?streams={streams}"

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

    @staticmethod
    def _parse(raw_message: str) -> dict | None:
        payload = json.loads(raw_message)
        data = payload.get("data", payload)
        if data.get("e") != "aggTrade":
            return None

        price = float(data["p"])
        quantity = float(data["q"])
        is_buyer_maker = bool(data["m"])
        side = "sell" if is_buyer_maker else "buy"

        return {
            "symbol": data["s"],
            "event_time": data["E"] / 1000,
            "price": price,
            "quantity": quantity,
            "quote_quantity": price * quantity,
            "side": side,
            "trade_id": data["a"],
        }
