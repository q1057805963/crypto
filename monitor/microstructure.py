import asyncio
import json
import logging
from collections import deque
from typing import Any

import websockets


class MarketMicrostructureState:
    def __init__(
        self,
        symbols: list[str],
        liquidations_enabled: bool = True,
        liquidation_feed_mode: str = "stream",
        liquidation_retention_seconds: int = 86400,
    ) -> None:
        self._depth: dict[str, dict[str, float]] = {}
        self._depth_history: dict[str, deque[tuple[float, float]]] = {}
        self._liquidations: dict[str, deque[dict[str, float | str]]] = {}
        self._last_depth_at: dict[str, float] = {}
        self._last_liquidation_at: dict[str, float] = {}
        self._last_liquidation_feed_at: dict[str, float] = {}
        self.liquidations_enabled = liquidations_enabled
        self.liquidation_feed_mode = liquidation_feed_mode
        self.liquidation_retention_seconds = max(int(liquidation_retention_seconds), 75)
        self.set_symbols(symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        wanted = {symbol.upper() for symbol in symbols}
        for symbol in wanted:
            self._depth.setdefault(symbol, {})
            self._depth_history.setdefault(symbol, deque())
            self._liquidations.setdefault(symbol, deque())
            self._last_depth_at.setdefault(symbol, 0.0)
            self._last_liquidation_at.setdefault(symbol, 0.0)
            self._last_liquidation_feed_at.setdefault(symbol, 0.0)

        for symbol in list(self._depth):
            if symbol not in wanted:
                self._depth.pop(symbol, None)
                self._depth_history.pop(symbol, None)
                self._liquidations.pop(symbol, None)
                self._last_depth_at.pop(symbol, None)
                self._last_liquidation_at.pop(symbol, None)
                self._last_liquidation_feed_at.pop(symbol, None)

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
        strongest_bid = max(
            (
                {"price": price, "notional": price * quantity}
                for price, quantity in bid_levels
            ),
            key=lambda item: item["notional"],
            default={"price": 0.0, "notional": 0.0},
        )
        strongest_ask = max(
            (
                {"price": price, "notional": price * quantity}
                for price, quantity in ask_levels
            ),
            key=lambda item: item["notional"],
            default={"price": 0.0, "notional": 0.0},
        )

        self._depth[symbol] = {
            "spread_bps": spread_bps,
            "bid_depth_notional": bid_depth_notional,
            "ask_depth_notional": ask_depth_notional,
            "depth_total_notional": total_depth_notional,
            "depth_imbalance": depth_imbalance,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "strongest_bid_wall_price": float(strongest_bid["price"]),
            "strongest_bid_wall_notional": float(strongest_bid["notional"]),
            "strongest_ask_wall_price": float(strongest_ask["price"]),
            "strongest_ask_wall_notional": float(strongest_ask["notional"]),
        }
        self._last_depth_at[symbol] = event_time

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
        self._last_liquidation_at[symbol] = event_time
        self._last_liquidation_feed_at[symbol] = max(
            self._last_liquidation_feed_at.get(symbol, 0.0),
            event_time,
        )
        cutoff = event_time - self.liquidation_retention_seconds
        while queue and float(queue[0]["event_time"]) < cutoff:
            queue.popleft()

    def mark_liquidation_feed(self, symbol: str, event_time: float) -> None:
        self._last_liquidation_feed_at[symbol.upper()] = event_time

    def snapshot(self, symbol: str, now: float) -> dict[str, Any]:
        symbol = symbol.upper()
        depth = dict(self._depth.get(symbol, {}))
        depth_history = self._depth_history.get(symbol, deque())
        liquidations = self._liquidations.get(symbol, deque())
        last_depth_at = float(self._last_depth_at.get(symbol, 0.0))
        last_liquidation_at = float(self._last_liquidation_at.get(symbol, 0.0))
        last_liquidation_feed_at = float(self._last_liquidation_feed_at.get(symbol, 0.0))

        cutoff = now - 60
        retention_cutoff = now - self.liquidation_retention_seconds
        while depth_history and depth_history[0][0] < cutoff:
            depth_history.popleft()
        while liquidations and float(liquidations[0]["event_time"]) < retention_cutoff:
            liquidations.popleft()

        current_depth = float(depth.get("depth_total_notional", 0.0))
        old_depth = depth_history[0][1] if depth_history else current_depth
        depth_drop_pct_1m = 0.0
        if old_depth > 0 and current_depth < old_depth:
            depth_drop_pct_1m = (old_depth - current_depth) / old_depth * 100

        long_liquidation_quote_1m = 0.0
        short_liquidation_quote_1m = 0.0
        liquidation_event_count_1m = 0
        for item in liquidations:
            if float(item["event_time"]) < cutoff:
                continue
            liquidation_event_count_1m += 1
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
        depth_age_seconds = now - last_depth_at if last_depth_at else None
        last_liquidation_age_seconds = now - last_liquidation_at if last_liquidation_at else None
        microstructure_status = (
            "active"
            if depth_age_seconds is not None and depth_age_seconds <= 20
            else "unavailable"
        )
        liquidation_feed_age_seconds = (
            now - last_liquidation_feed_at if last_liquidation_feed_at else None
        )
        if self.liquidation_feed_mode == "poll":
            liquidation_feed_active = (
                liquidation_feed_age_seconds is not None
                and liquidation_feed_age_seconds <= 45
            )
        else:
            liquidation_feed_active = microstructure_status == "active"

        if not self.liquidations_enabled or not liquidation_feed_active:
            liquidation_data_status = "unavailable"
        elif liquidation_event_count_1m:
            liquidation_data_status = "recent_event"
        else:
            liquidation_data_status = "no_recent_event"

        return {
            "spread_bps": float(depth.get("spread_bps", 0.0)),
            "bid_depth_notional": float(depth.get("bid_depth_notional", 0.0)),
            "ask_depth_notional": float(depth.get("ask_depth_notional", 0.0)),
            "depth_total_notional": current_depth,
            "depth_imbalance": float(depth.get("depth_imbalance", 0.0)),
            "depth_drop_pct_1m": depth_drop_pct_1m,
            "bid_wall_price": float(depth.get("strongest_bid_wall_price", 0.0)),
            "bid_wall_notional": float(depth.get("strongest_bid_wall_notional", 0.0)),
            "ask_wall_price": float(depth.get("strongest_ask_wall_price", 0.0)),
            "ask_wall_notional": float(depth.get("strongest_ask_wall_notional", 0.0)),
            "long_liquidation_quote_1m": long_liquidation_quote_1m,
            "short_liquidation_quote_1m": short_liquidation_quote_1m,
            "liquidation_total_quote_1m": total_liquidation_quote_1m,
            "liquidation_imbalance": liquidation_imbalance,
            "liquidation_event_count_1m": liquidation_event_count_1m,
            "liquidation_data_status": liquidation_data_status,
            "microstructure_status": microstructure_status,
            "depth_data_age_seconds": depth_age_seconds,
            "last_liquidation_age_seconds": last_liquidation_age_seconds,
        }

    def liquidation_summary(self, symbol: str, seconds: int, now: float | None = None) -> dict[str, Any]:
        symbol = symbol.upper()
        now = float(now or 0) or max(
            self._last_depth_at.get(symbol, 0.0),
            self._last_liquidation_feed_at.get(symbol, 0.0),
        )
        if now <= 0:
            return {
                "period_liquidation_data_status": "unavailable",
                "period_liquidation_event_count": 0,
                "period_long_liquidation_quote": 0.0,
                "period_short_liquidation_quote": 0.0,
                "period_liquidation_total_quote": 0.0,
                "period_liquidation_imbalance": 0.0,
            }

        seconds = max(int(seconds), 60)
        cutoff = now - seconds
        liquidations = self._liquidations.get(symbol, deque())
        last_depth_at = float(self._last_depth_at.get(symbol, 0.0))
        last_liquidation_feed_at = float(self._last_liquidation_feed_at.get(symbol, 0.0))

        if self.liquidation_feed_mode == "poll":
            feed_active = now - last_liquidation_feed_at <= 45 if last_liquidation_feed_at else False
        else:
            feed_active = now - last_depth_at <= 20 if last_depth_at else False

        long_quote = 0.0
        short_quote = 0.0
        event_count = 0
        for item in liquidations:
            event_time = float(item["event_time"])
            if event_time < cutoff or event_time > now:
                continue
            event_count += 1
            quote_quantity = float(item["quote_quantity"])
            if str(item["side"]).upper() == "SELL":
                long_quote += quote_quantity
            else:
                short_quote += quote_quantity

        total_quote = long_quote + short_quote
        imbalance = (short_quote - long_quote) / total_quote if total_quote else 0.0
        if not self.liquidations_enabled or not feed_active:
            status = "unavailable"
        elif event_count:
            status = "recent_event"
        else:
            status = "no_recent_event"

        return {
            "period_liquidation_data_status": status,
            "period_liquidation_event_count": event_count,
            "period_long_liquidation_quote": round(long_quote, 2),
            "period_short_liquidation_quote": round(short_quote, 2),
            "period_liquidation_total_quote": round(total_quote, 2),
            "period_liquidation_imbalance": round(imbalance, 4),
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
        # 中文合约名无法放进 URL 的 streams 参数，改为连接后发 SUBSCRIBE 消息
        streams = []
        for symbol in self.symbols:
            symbol_name = symbol.lower()
            streams.append(f"{symbol_name}@depth{self.depth_levels}@{self.depth_interval}")
            streams.append(f"{symbol_name}@forceOrder")
        self._streams = streams
        self.url = "wss://fstream.binance.com/stream"

    async def run(self, state: MarketMicrostructureState) -> None:
        backoff_seconds = 1
        while True:
            streams = self._streams
            try:
                logging.info("Connecting to Binance Futures microstructure streams")
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {"method": "SUBSCRIBE", "params": streams, "id": 1},
                            ensure_ascii=False,
                        )
                    )
                    backoff_seconds = 1
                    while True:
                        if streams != self._streams:
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
