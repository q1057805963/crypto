import asyncio
import json
import logging
from collections import deque
from typing import Any

import websockets


class MarketMicrostructureState:
    def __init__(self, symbols: list[str]) -> None:
        self._depth: dict[str, dict[str, float]] = {}
        self._depth_history: dict[str, deque[tuple[float, float]]] = {}
        self._liquidations: dict[str, deque[dict[str, float | str]]] = {}
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        wanted = {symbol.upper() for symbol in symbols}
        for symbol in wanted:
            self._depth.setdefault(symbol, {})
            self._depth_history.setdefault(symbol, deque())
            self._liquidations.setdefault(symbol, deque())

        for symbol in list(self._depth):
            if symbol not in wanted:
                self._depth.pop(symbol, None)
                self._depth_history.pop(symbol, None)
                self._liquidations.pop(symbol, None)

    def record_depth(
        self,
        symbol: str,
        event_time: float,
        bids: list[list[str]],
        asks: list[list[str]],
    ) -> None:
        bid_levels = [(float(price), float(quantity)) for price, quantity in bids]
        ask_levels = [(float(price), float(quantity)) for price, quantity in asks]

        best_bid = bid_levels[0][0] if bid_levels else 0.0
        best_ask = ask_levels[0][0] if ask_levels else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread_bps = ((best_ask - best_bid) / mid * 10000) if mid else 0.0

        bid_depth_notional = sum(price * quantity for price, quantity in bid_levels)
        ask_depth_notional = sum(price * quantity for price, quantity in ask_levels)
        total_depth_notional = bid_depth_notional + ask_depth_notional
        depth_imbalance = (
            (bid_depth_notional - ask_depth_notional) / total_depth_notional
            if total_depth_notional
            else 0.0
        )

        self._depth[symbol] = {
            "spread_bps": spread_bps,
            "bid_depth_notional": bid_depth_notional,
            "ask_depth_notional": ask_depth_notional,
            "depth_total_notional": total_depth_notional,
            "depth_imbalance": depth_imbalance,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }

        history = self._depth_history[symbol]
        history.append((event_time, total_depth_notional))
        cutoff = event_time - 75
        while history and history[0][0] < cutoff:
            history.popleft()

    def record_liquidation(
        self,
        symbol: str,
        event_time: float,
        side: str,
        price: float,
        quantity: float,
    ) -> None:
        quote_quantity = price * quantity
        queue = self._liquidations[symbol]
        queue.append(
            {
                "event_time": event_time,
                "side": side.upper(),
                "quote_quantity": quote_quantity,
            }
        )
        cutoff = event_time - 75
        while queue and float(queue[0]["event_time"]) < cutoff:
            queue.popleft()

    def snapshot(self, symbol: str, now: float) -> dict[str, float]:
        symbol = symbol.upper()
        depth = dict(self._depth.get(symbol, {}))
        depth_history = self._depth_history.get(symbol, deque())
        liquidations = self._liquidations.get(symbol, deque())

        cutoff = now - 60
        while depth_history and depth_history[0][0] < cutoff:
            depth_history.popleft()
        while liquidations and float(liquidations[0]["event_time"]) < cutoff:
            liquidations.popleft()

        current_depth = float(depth.get("depth_total_notional", 0.0))
        old_depth = depth_history[0][1] if depth_history else current_depth
        depth_drop_pct_1m = 0.0
        if old_depth > 0 and current_depth < old_depth:
            depth_drop_pct_1m = (old_depth - current_depth) / old_depth * 100

        long_liquidation_quote_1m = 0.0
        short_liquidation_quote_1m = 0.0
        for item in liquidations:
            quote_quantity = float(item["quote_quantity"])
            # SELL forced orders usually correspond to long liquidation unwinds.
            if str(item["side"]).upper() == "SELL":
                long_liquidation_quote_1m += quote_quantity
            else:
                short_liquidation_quote_1m += quote_quantity

        total_liquidation_quote_1m = long_liquidation_quote_1m + short_liquidation_quote_1m
        liquidation_imbalance = (
            (short_liquidation_quote_1m - long_liquidation_quote_1m)
            / total_liquidation_quote_1m
            if total_liquidation_quote_1m
            else 0.0
        )

        return {
            "spread_bps": float(depth.get("spread_bps", 0.0)),
            "bid_depth_notional": float(depth.get("bid_depth_notional", 0.0)),
            "ask_depth_notional": float(depth.get("ask_depth_notional", 0.0)),
            "depth_total_notional": current_depth,
            "depth_imbalance": float(depth.get("depth_imbalance", 0.0)),
            "depth_drop_pct_1m": depth_drop_pct_1m,
            "long_liquidation_quote_1m": long_liquidation_quote_1m,
            "short_liquidation_quote_1m": short_liquidation_quote_1m,
            "liquidation_total_quote_1m": total_liquidation_quote_1m,
            "liquidation_imbalance": liquidation_imbalance,
        }


class BinanceFuturesMicrostructureStream:
    def __init__(
        self,
        symbols: list[str],
        depth_levels: int = 10,
        depth_interval: str = "500ms",
    ) -> None:
        self.depth_levels = depth_levels
        self.depth_interval = depth_interval
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = [symbol.upper() for symbol in symbols]
        streams = []
        for symbol in self.symbols:
            symbol_name = symbol.lower()
            streams.append(f"{symbol_name}@depth{self.depth_levels}@{self.depth_interval}")
            streams.append(f"{symbol_name}@forceOrder")
        self.url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    async def run(self, state: MarketMicrostructureState) -> None:
        backoff_seconds = 1
        while True:
            url = self.url
            try:
                logging.info("Connecting to Binance Futures microstructure streams")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    backoff_seconds = 1
                    while True:
                        if url != self.url:
                            logging.info("Microstructure symbol list changed, reconnecting stream")
                            break

                        try:
                            raw_message = await asyncio.wait_for(websocket.recv(), timeout=5)
                        except TimeoutError:
                            continue

                        self._handle_message(raw_message, state)
            except Exception as exc:
                logging.warning(
                    "Microstructure stream disconnected: %s. Reconnecting in %ss",
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    def _handle_message(self, raw_message: str, state: MarketMicrostructureState) -> None:
        payload = json.loads(raw_message)
        data = payload.get("data", payload)
        event_type = data.get("e")

        if event_type == "depthUpdate":
            symbol = data["s"].upper()
            state.record_depth(
                symbol=symbol,
                event_time=float(data["E"]) / 1000,
                bids=data.get("b", []),
                asks=data.get("a", []),
            )
            return

        if event_type == "forceOrder":
            order: dict[str, Any] = data.get("o", {})
            symbol = str(order.get("s", "")).upper()
            if not symbol:
                return

            state.record_liquidation(
                symbol=symbol,
                event_time=float(data["E"]) / 1000,
                side=str(order.get("S", "SELL")),
                price=float(order.get("ap") or order.get("p") or 0.0),
                quantity=float(order.get("z") or order.get("q") or 0.0),
            )
