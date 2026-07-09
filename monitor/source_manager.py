import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.microstructure import BinanceFuturesMicrostructureStream, MarketMicrostructureState
from monitor.okx_rest import OkxSwapTickerPoller
from monitor.okx_ws import OkxSwapWebSocketStream


def normalized_exchange(config: dict) -> str:
    return str(config.get("exchange", "okx_swap")).strip().lower()


def is_okx_exchange(exchange: str) -> bool:
    return exchange in {"okx", "okx_swap", "okx_usdt_swap"}


def normalized_data_source(exchange: str, data_source: str) -> str:
    source = str(data_source or "").strip().lower()
    if is_okx_exchange(exchange):
        if source in {"", "auto"}:
            return "websocket"
        if source not in {"rest", "websocket"}:
            logging.warning(
                "Unsupported OKX data_source=%s, fallback to websocket",
                data_source,
            )
            return "websocket"
        return source
    if source in {"", "auto"}:
        return "websocket"
    if source not in {"rest", "websocket"}:
        logging.warning("Unsupported data_source=%s, fallback to websocket", data_source)
        return "websocket"
    return source


@dataclass(frozen=True)
class SourceSpec:
    exchange: str
    data_source: str


@dataclass
class SourceContext:
    exchange: str
    data_source: str
    stream: object
    microstructure_state: MarketMicrostructureState
    microstructure_stream: BinanceFuturesMicrostructureStream | None


def source_label(exchange: str, data_source: str) -> str:
    venue = "OKX" if is_okx_exchange(exchange) else "Binance"
    transport = "REST" if data_source == "rest" else "WebSocket"
    return f"{venue} {transport}"


def build_source_specs(config: dict, exchange: str, data_source: str) -> list[SourceSpec]:
    failover = config.get("failover", {})
    enabled = bool(failover.get("enabled", False))
    raw_candidates = list(failover.get("candidates") or [])
    if enabled and not raw_candidates:
        raw_candidates = [
            {"exchange": exchange, "data_source": data_source},
            {"exchange": "okx_swap", "data_source": "rest"},
        ]
    elif not enabled:
        raw_candidates = [{"exchange": exchange, "data_source": data_source}]

    specs: list[SourceSpec] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_candidates:
        item_exchange = str(raw.get("exchange") or exchange).strip().lower()
        item_source = normalized_data_source(
            item_exchange,
            str(raw.get("data_source") or data_source),
        )
        key = (item_exchange, item_source)
        if key in seen:
            continue
        seen.add(key)
        specs.append(SourceSpec(exchange=item_exchange, data_source=item_source))

    primary = SourceSpec(
        exchange=exchange,
        data_source=normalized_data_source(exchange, data_source),
    )
    if primary not in specs:
        specs.insert(0, primary)
    return specs


def build_source_context(config: dict, symbols: list[str], spec: SourceSpec) -> SourceContext:
    microstructure_config = config.get("microstructure", {})
    microstructure_state = MarketMicrostructureState(
        symbols,
        liquidations_enabled=(
            bool(microstructure_config.get("enabled", True))
            if is_okx_exchange(spec.exchange)
            else True
        ),
        liquidation_feed_mode=(
            "poll"
            if is_okx_exchange(spec.exchange) and spec.data_source == "rest"
            else "stream"
        ),
        liquidation_retention_seconds=int(
            microstructure_config.get("liquidation_retention_seconds", 86400)
        ),
    )
    microstructure_stream = None
    if microstructure_config.get("enabled", True) and not is_okx_exchange(spec.exchange):
        microstructure_stream = BinanceFuturesMicrostructureStream(
            symbols,
            depth_levels=int(microstructure_config.get("depth_levels", 10)),
            depth_interval=str(microstructure_config.get("depth_interval", "500ms")),
        )

    if is_okx_exchange(spec.exchange) and spec.data_source == "websocket":
        stream = OkxSwapWebSocketStream(
            symbols,
            liquidation_poll_interval_seconds=float(
                microstructure_config.get("rest_liquidation_poll_interval_seconds", 15)
            ),
            microstructure_state=(
                microstructure_state
                if microstructure_config.get("enabled", True)
                else None
            ),
        )
    elif is_okx_exchange(spec.exchange):
        stream = OkxSwapTickerPoller(
            symbols,
            poll_interval_seconds=float(config.get("rest_poll_interval_seconds", 2)),
            per_symbol_delay_ms=int(config.get("rest_per_symbol_delay_ms", 150)),
            oi_poll_interval_seconds=float(config.get("oi_poll_interval_seconds", 30)),
            funding_poll_interval_seconds=float(
                config.get("funding_poll_interval_seconds", 60)
            ),
            depth_poll_interval_seconds=float(
                microstructure_config.get(
                    "rest_depth_poll_interval_seconds",
                    config.get("rest_poll_interval_seconds", 5),
                )
            ),
            liquidation_poll_interval_seconds=float(
                microstructure_config.get("rest_liquidation_poll_interval_seconds", 15)
            ),
            microstructure_state=(
                microstructure_state
                if microstructure_config.get("enabled", True)
                else None
            ),
        )
    elif spec.data_source == "websocket":
        stream = BinanceFuturesAggTradeStream(
            symbols,
            microstructure_state=microstructure_state,
        )
    else:
        stream = BinanceFuturesTickerPoller(
            symbols,
            poll_interval_seconds=float(config.get("rest_poll_interval_seconds", 2)),
            per_symbol_delay_ms=int(config.get("rest_per_symbol_delay_ms", 150)),
            oi_poll_interval_seconds=float(config.get("oi_poll_interval_seconds", 30)),
            funding_poll_interval_seconds=float(
                config.get("funding_poll_interval_seconds", 60)
            ),
            liquidation_poll_interval_seconds=float(
                microstructure_config.get("rest_liquidation_poll_interval_seconds", 15)
            ),
            microstructure_state=microstructure_state,
        )

    return SourceContext(
        exchange=spec.exchange,
        data_source=spec.data_source,
        stream=stream,
        microstructure_state=microstructure_state,
        microstructure_stream=microstructure_stream,
    )


class SourceFailoverManager:
    def __init__(
        self,
        *,
        config: dict,
        specs: list[SourceSpec],
        symbols: list[str],
        stale_after_seconds: float,
        switch_cooldown_seconds: float,
        primary_retry_seconds: float = 300.0,
        on_switch: Callable[[str, str, str], None] | None = None,
        instrument_directories=None,
    ) -> None:
        self.config = config
        self.specs = specs
        self.instrument_directories = instrument_directories
        self._lock = threading.Lock()
        self.symbols = list(symbols)
        self.stale_after_seconds = max(float(stale_after_seconds), 5.0)
        self.switch_cooldown_seconds = max(float(switch_cooldown_seconds), 0.0)
        self.primary_retry_seconds = max(float(primary_retry_seconds), 0.0)
        self.on_switch = on_switch
        self._active_index = 0
        self._active_context: SourceContext | None = None
        self._reader_task: asyncio.Task | None = None
        self._micro_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
        self._nonce = 0
        self._last_trade_at = 0.0
        self._last_switch_at = 0.0
        self._reload_requested = False
        self._supplement_task: asyncio.Task | None = None

    @property
    def active_exchange(self) -> str:
        if self._active_context:
            return self._active_context.exchange
        return self.specs[self._active_index].exchange

    @property
    def active_data_source(self) -> str:
        if self._active_context:
            return self._active_context.data_source
        return self.specs[self._active_index].data_source

    def liquidation_summary(self, symbol: str, seconds: int) -> dict:
        if not self._active_context:
            return {}
        return self._active_context.microstructure_state.liquidation_summary(
            symbol,
            seconds,
            now=time.time(),
        )

    def source_health(self) -> dict:
        if not self._active_context:
            return {}
        stream = self._active_context.stream
        health = stream.health_summary() if hasattr(stream, "health_summary") else {}
        if not isinstance(health, dict):
            health = {}
        if not health:
            age_seconds = max(0.0, time.monotonic() - self._last_trade_at)
            status = "active" if age_seconds <= self.stale_after_seconds else "stale"
            health = {
                "status": status,
                "active_count": 1 if status == "active" else 0,
                "total_count": 1,
                "channels": [
                    {
                        "key": "market",
                        "label": "行情",
                        "status": status,
                        "age_seconds": age_seconds,
                    }
                ],
            }
        return {
            "exchange": self._active_context.exchange,
            "data_source": self._active_context.data_source,
            "label": source_label(
                self._active_context.exchange,
                self._active_context.data_source,
            ),
            **health,
        }

    def exchange_for_symbol(self, symbol: str) -> str:
        """返回该标的应使用的交易所（用于 K 线等按标的选源场景）。"""
        symbol = str(symbol).upper()
        if not self.instrument_directories:
            return self.active_exchange
        primary_exchange = self.active_exchange
        if self.instrument_directories._directory_for(primary_exchange).supports(symbol) is not False:
            return primary_exchange
        other = "binance" if is_okx_exchange(primary_exchange) else "okx_swap"
        if self.instrument_directories._directory_for(other).supports(symbol) is not False:
            return other
        return primary_exchange

    def get_symbols(self) -> list[str]:
        with self._lock:
            return list(self.symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        next_symbols = [str(symbol).upper() for symbol in symbols if str(symbol).strip()]
        with self._lock:
            previous_symbols = list(self.symbols)
            if previous_symbols == next_symbols:
                return
            self.symbols = next_symbols
            if self._active_context:
                self._reload_requested = True

        if self._active_context:
            logging.info(
                "Symbol list changed, reconnecting active source with %s",
                ", ".join(next_symbols),
            )

    def _take_reload_request(self) -> bool:
        with self._lock:
            requested = self._reload_requested
            self._reload_requested = False
            return requested

    async def _reload_if_requested(self) -> None:
        if not self._active_context or not self._take_reload_request():
            return
        await self._activate(
            self._active_index,
            "监控列表已更新，重建数据源连接",
        )

    async def _stop_active(self) -> None:
        tasks = [task for task in (self._reader_task, self._micro_task, self._supplement_task) if task]
        self._reader_task = None
        self._micro_task = None
        self._supplement_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _activate(self, index: int, note: str = "") -> None:
        await self._stop_active()
        self._active_index = index
        spec = self.specs[index]
        symbols = self.get_symbols()
        skipped: list[str] = []
        if self.instrument_directories:
            allowed, skipped = self.instrument_directories.filter_for_exchange(
                spec.exchange, symbols
            )
            if skipped:
                logging.info(
                    "%s 无以下合约，该数据源期间暂停订阅: %s",
                    source_label(spec.exchange, spec.data_source),
                    ", ".join(skipped),
                )
                symbols = allowed
        self._active_context = build_source_context(self.config, symbols, spec)
        self._nonce += 1
        current_nonce = self._nonce
        self._last_trade_at = time.monotonic()

        async def _pump() -> None:
            async for trade in self._active_context.stream.listen():
                await self._queue.put((current_nonce, trade))

        self._reader_task = asyncio.create_task(_pump())
        if self._active_context.microstructure_stream:
            self._micro_task = asyncio.create_task(
                self._active_context.microstructure_stream.run(
                    self._active_context.microstructure_state
                )
            )
        self._start_supplement(spec, skipped if self.instrument_directories else [], current_nonce)
        if self.on_switch:
            self.on_switch(
                self._active_context.exchange,
                self._active_context.data_source,
                note,
            )
        logging.info(
            "Active source: %s",
            source_label(
                self._active_context.exchange,
                self._active_context.data_source,
            ),
        )

    async def _switch_to_next(self, reason: str) -> None:
        if len(self.specs) <= 1:
            return
        now = time.monotonic()
        if now - self._last_switch_at < self.switch_cooldown_seconds:
            return
        next_index = (self._active_index + 1) % len(self.specs)
        current_label = source_label(self.active_exchange, self.active_data_source)
        next_spec = self.specs[next_index]
        next_label = source_label(next_spec.exchange, next_spec.data_source)
        note = f"{current_label} 异常，已切换到 {next_label}"
        logging.warning("Source failover: %s -> %s (%s)", current_label, next_label, reason)
        self._last_switch_at = now
        await self._activate(next_index, note)

    async def _maybe_retry_primary(self) -> None:
        if (
            self.primary_retry_seconds <= 0
            or self._active_index == 0
            or len(self.specs) <= 1
        ):
            return
        now = time.monotonic()
        if now - self._last_switch_at < self.primary_retry_seconds:
            return
        primary = self.specs[0]
        primary_label = source_label(primary.exchange, primary.data_source)
        logging.info("Retrying primary source: %s", primary_label)
        self._last_switch_at = now
        await self._activate(0, f"尝试切回主数据源 {primary_label}")

    async def listen(self):
        await self._activate(self._active_index, "")
        timeout_seconds = min(max(self.stale_after_seconds / 3, 1.0), 5.0)
        while True:
            await self._reload_if_requested()
            await self._maybe_retry_primary()
            try:
                nonce, trade = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=timeout_seconds,
                )
                if nonce != self._nonce:
                    continue
                await self._reload_if_requested()
                self._last_trade_at = time.monotonic()
                yield trade
            except asyncio.TimeoutError:
                await self._reload_if_requested()
                if time.monotonic() - self._last_trade_at >= self.stale_after_seconds:
                    await self._switch_to_next(
                        f"no trades for {int(time.monotonic() - self._last_trade_at)}s"
                    )

    def _start_supplement(self, primary_spec: SourceSpec, skipped: list[str], nonce: int) -> None:
        if not skipped:
            return
        if is_okx_exchange(primary_spec.exchange):
            supplement_exchange = "binance"
        else:
            supplement_exchange = "okx_swap"
        if self.instrument_directories:
            can_supplement, _ = self.instrument_directories.filter_for_exchange(
                supplement_exchange, skipped
            )
        else:
            can_supplement = skipped
        if not can_supplement:
            return

        poll_interval = float(self.config.get("rest_poll_interval_seconds", 5))
        per_symbol_delay = int(self.config.get("rest_per_symbol_delay_ms", 150))
        oi_interval = float(self.config.get("oi_poll_interval_seconds", 30))
        funding_interval = float(self.config.get("funding_poll_interval_seconds", 60))

        if is_okx_exchange(supplement_exchange):
            micro_config = self.config.get("microstructure", {})
            poller = OkxSwapTickerPoller(
                can_supplement,
                poll_interval_seconds=poll_interval,
                per_symbol_delay_ms=per_symbol_delay,
                oi_poll_interval_seconds=oi_interval,
                funding_poll_interval_seconds=funding_interval,
                depth_poll_interval_seconds=float(
                    micro_config.get("rest_depth_poll_interval_seconds", poll_interval)
                ),
                liquidation_poll_interval_seconds=float(
                    micro_config.get("rest_liquidation_poll_interval_seconds", 15)
                ),
                microstructure_state=(
                    self._active_context.microstructure_state
                    if self._active_context and micro_config.get("enabled", True)
                    else None
                ),
            )
        else:
            micro_config = self.config.get("microstructure", {})
            poller = BinanceFuturesTickerPoller(
                can_supplement,
                poll_interval_seconds=poll_interval,
                per_symbol_delay_ms=per_symbol_delay,
                oi_poll_interval_seconds=oi_interval,
                funding_poll_interval_seconds=funding_interval,
                liquidation_poll_interval_seconds=float(
                    micro_config.get("rest_liquidation_poll_interval_seconds", 15)
                ),
                microstructure_state=(
                    self._active_context.microstructure_state
                    if self._active_context
                    else None
                ),
            )

        queue = self._queue

        async def _supplement_pump() -> None:
            async for trade in poller.listen():
                await queue.put((nonce, trade))

        self._supplement_task = asyncio.create_task(_supplement_pump())
        supplement_label = source_label(supplement_exchange, "rest")
        logging.info(
            "Supplementary source: %s for %s",
            supplement_label,
            ", ".join(can_supplement),
        )

    async def close(self) -> None:
        await self._stop_active()
