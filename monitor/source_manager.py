import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.microstructure import BinanceFuturesMicrostructureStream, MarketMicrostructureState
from monitor.okx_rest import OkxSwapTickerPoller


def normalized_exchange(config: dict) -> str:
    return str(config.get("exchange", "binance_usdm")).strip().lower()


def is_okx_exchange(exchange: str) -> bool:
    return exchange in {"okx", "okx_swap", "okx_usdt_swap"}


def normalized_data_source(exchange: str, data_source: str) -> str:
    source = str(data_source or "").strip().lower()
    if is_okx_exchange(exchange):
        if source not in {"", "auto", "rest"}:
            logging.warning(
                "OKX exchange currently uses REST polling; ignoring data_source=%s",
                data_source,
            )
        return "rest"
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
        liquidation_feed_mode="poll" if is_okx_exchange(spec.exchange) else "stream",
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

    if is_okx_exchange(spec.exchange):
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
        on_switch: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.specs = specs
        self.symbols = list(symbols)
        self.stale_after_seconds = max(float(stale_after_seconds), 5.0)
        self.switch_cooldown_seconds = max(float(switch_cooldown_seconds), 0.0)
        self.on_switch = on_switch
        self._active_index = 0
        self._active_context: SourceContext | None = None
        self._reader_task: asyncio.Task | None = None
        self._micro_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
        self._nonce = 0
        self._last_trade_at = 0.0
        self._last_switch_at = 0.0

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

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        if not self._active_context:
            return
        self._active_context.stream.set_symbols(self.symbols)
        self._active_context.microstructure_state.set_symbols(self.symbols)
        if self._active_context.microstructure_stream:
            self._active_context.microstructure_stream.set_symbols(self.symbols)

    async def _stop_active(self) -> None:
        tasks = [task for task in (self._reader_task, self._micro_task) if task]
        self._reader_task = None
        self._micro_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _activate(self, index: int, note: str = "") -> None:
        await self._stop_active()
        self._active_index = index
        self._active_context = build_source_context(self.config, self.symbols, self.specs[index])
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

    async def listen(self):
        await self._activate(self._active_index, "")
        timeout_seconds = min(max(self.stale_after_seconds / 3, 1.0), 5.0)
        while True:
            try:
                nonce, trade = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=timeout_seconds,
                )
                if nonce != self._nonce:
                    continue
                self._last_trade_at = time.monotonic()
                yield trade
            except asyncio.TimeoutError:
                if time.monotonic() - self._last_trade_at >= self.stale_after_seconds:
                    await self._switch_to_next(
                        f"no trades for {int(time.monotonic() - self._last_trade_at)}s"
                    )

    async def close(self) -> None:
        await self._stop_active()
