import argparse
import asyncio
import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from monitor.auth import AuthManager
from monitor.ai_analysis import AIAnalyzer, summarize_analysis
from monitor.alert import ConsoleAlert
from monitor.anomaly import AnomalyDetector, AnomalyEvent
from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.dashboard import DashboardServer, DashboardState
from monitor.microstructure import BinanceFuturesMicrostructureStream, MarketMicrostructureState
from monitor.okx_rest import OkxSwapTickerPoller
from monitor.storage import AlertStore
from monitor.telegram import TelegramAlert, normalize_telegram_users
from monitor.telegram_bot import TelegramBotResponder
from monitor.timeframe_analysis import TimeframeAnalysisService
from monitor.user_config import UserConfigStore


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def save_config(path: Path, config: dict) -> None:
    public_config = {key: value for key, value in config.items() if not key.startswith("_")}
    if config.get("_ai_api_key_from_env") or config.get("_ai_api_key_runtime_only"):
        ai = dict(public_config.get("ai", {}))
        ai["api_key"] = ""
        public_config["ai"] = ai
    if config.get("_auth_secret_from_env"):
        auth = dict(public_config.get("auth", {}))
        auth["jwt_secret"] = ""
        public_config["auth"] = auth
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(public_config, file, allow_unicode=True, sort_keys=False)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _migrate_telegram_config(telegram: dict) -> dict:
    """Migrate legacy chat_id/chat_ids into per-user Telegram configs."""
    if "chat_id" in telegram and "chat_ids" not in telegram:
        old = str(telegram.pop("chat_id", ""))
        telegram["chat_ids"] = [old] if old.strip() else []
    elif "chat_id" in telegram:
        telegram.pop("chat_id", None)
    if "chat_ids" not in telegram:
        telegram["chat_ids"] = []
    telegram["users"] = normalize_telegram_users(
        telegram.get("users"),
        str(telegram.get("bot_token", "")),
        telegram.get("chat_ids", []),
    )
    return telegram


def apply_env_overrides(config: dict) -> dict:
    config = dict(config)
    telegram = dict(config.get("telegram", {}))
    dashboard = dict(config.get("dashboard", {}))
    ai = dict(config.get("ai", {}))
    auth = dict(config.get("auth", {}))
    microstructure = dict(config.get("microstructure", {}))
    telegram_bot = dict(config.get("telegram_bot", {}))

    if "CFM_EXCHANGE" in os.environ:
        config["exchange"] = os.environ["CFM_EXCHANGE"]
    if "CFM_DATA_SOURCE" in os.environ:
        config["data_source"] = os.environ["CFM_DATA_SOURCE"]
    if "CFM_REST_POLL_INTERVAL_SECONDS" in os.environ:
        config["rest_poll_interval_seconds"] = float(os.environ["CFM_REST_POLL_INTERVAL_SECONDS"])
    if "CFM_REST_PER_SYMBOL_DELAY_MS" in os.environ:
        config["rest_per_symbol_delay_ms"] = int(os.environ["CFM_REST_PER_SYMBOL_DELAY_MS"])
    if "CFM_OI_POLL_INTERVAL_SECONDS" in os.environ:
        config["oi_poll_interval_seconds"] = float(os.environ["CFM_OI_POLL_INTERVAL_SECONDS"])
    if "CFM_FUNDING_POLL_INTERVAL_SECONDS" in os.environ:
        config["funding_poll_interval_seconds"] = float(os.environ["CFM_FUNDING_POLL_INTERVAL_SECONDS"])

    telegram = _migrate_telegram_config(telegram)

    telegram["enabled"] = _env_flag(
        "CFM_TELEGRAM_ENABLED",
        bool(telegram.get("enabled", False)),
    )
    telegram["bot_token"] = os.environ.get(
        "CFM_TELEGRAM_BOT_TOKEN",
        str(telegram.get("bot_token", "")),
    )
    env_chat_ids = os.environ.get("CFM_TELEGRAM_CHAT_IDS")
    if env_chat_ids is not None:
        telegram["chat_ids"] = [cid.strip() for cid in env_chat_ids.split(",") if cid.strip()]
    if not telegram.get("users") and (telegram.get("bot_token") or telegram.get("chat_ids")):
        telegram["users"] = normalize_telegram_users(
            None,
            str(telegram.get("bot_token", "")),
            telegram.get("chat_ids", []),
        )

    dashboard["host"] = os.environ.get(
        "CFM_DASHBOARD_HOST",
        str(dashboard.get("host", "127.0.0.1")),
    )
    dashboard["port"] = int(
        os.environ.get(
            "CFM_DASHBOARD_PORT",
            str(dashboard.get("port", 8765)),
        )
    )

    auth["enabled"] = _env_flag("CFM_AUTH_ENABLED", bool(auth.get("enabled", True)))
    auth["allow_registration"] = _env_flag(
        "CFM_AUTH_ALLOW_REGISTRATION",
        bool(auth.get("allow_registration", False)),
    )
    if "CFM_AUTH_TOKEN_TTL_SECONDS" in os.environ:
        auth["token_ttl_seconds"] = int(os.environ["CFM_AUTH_TOKEN_TTL_SECONDS"])
    if "CFM_AUTH_USERS_PATH" in os.environ:
        auth["users_path"] = os.environ["CFM_AUTH_USERS_PATH"]
    if "CFM_AUTH_SECRET_PATH" in os.environ:
        auth["secret_path"] = os.environ["CFM_AUTH_SECRET_PATH"]
    if os.environ.get("CFM_AUTH_SECRET"):
        auth["jwt_secret"] = os.environ["CFM_AUTH_SECRET"]
        config["_auth_secret_from_env"] = True

    if "CFM_MICROSTRUCTURE_ENABLED" in os.environ:
        microstructure["enabled"] = _env_flag(
            "CFM_MICROSTRUCTURE_ENABLED",
            bool(microstructure.get("enabled", True)),
        )
    if "CFM_REST_DEPTH_POLL_INTERVAL_SECONDS" in os.environ:
        microstructure["rest_depth_poll_interval_seconds"] = float(
            os.environ["CFM_REST_DEPTH_POLL_INTERVAL_SECONDS"]
        )
    if "CFM_REST_LIQUIDATION_POLL_INTERVAL_SECONDS" in os.environ:
        microstructure["rest_liquidation_poll_interval_seconds"] = float(
            os.environ["CFM_REST_LIQUIDATION_POLL_INTERVAL_SECONDS"]
        )

    if "CFM_TELEGRAM_BOT_RESPONDER_ENABLED" in os.environ:
        telegram_bot["enabled"] = _env_flag(
            "CFM_TELEGRAM_BOT_RESPONDER_ENABLED",
            bool(telegram_bot.get("enabled", True)),
        )
    if "CFM_TELEGRAM_BOT_POLL_INTERVAL_SECONDS" in os.environ:
        telegram_bot["poll_interval_seconds"] = float(
            os.environ["CFM_TELEGRAM_BOT_POLL_INTERVAL_SECONDS"]
        )
    if "CFM_TELEGRAM_BOT_REQUEST_TIMEOUT_SECONDS" in os.environ:
        telegram_bot["request_timeout_seconds"] = int(
            os.environ["CFM_TELEGRAM_BOT_REQUEST_TIMEOUT_SECONDS"]
        )
    if "CFM_TELEGRAM_BOT_AI_COOLDOWN_SECONDS" in os.environ:
        telegram_bot["ai_cooldown_seconds"] = int(
            os.environ["CFM_TELEGRAM_BOT_AI_COOLDOWN_SECONDS"]
        )

    ai_key = os.environ.get("CFM_AI_API_KEY")
    if ai_key is None:
        ai_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if ai_key is not None:
        ai["api_key"] = ai_key
        config["_ai_api_key_from_env"] = True
    elif ai.get("api_key"):
        config["_ai_api_key_runtime_only"] = True

    if "CFM_AI_ENABLED" in os.environ:
        ai["enabled"] = _env_flag("CFM_AI_ENABLED", bool(ai.get("enabled", False)))

    provider_from_env = os.environ.get("CFM_AI_PROVIDER")
    if provider_from_env:
        ai["provider"] = provider_from_env
    elif os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_MODEL"):
        ai["provider"] = "anthropic"

    ai["base_url"] = os.environ.get(
        "CFM_AI_BASE_URL",
        os.environ.get("ANTHROPIC_BASE_URL", str(ai.get("base_url", ""))),
    )
    ai["model"] = os.environ.get(
        "CFM_AI_MODEL",
        os.environ.get("ANTHROPIC_MODEL", str(ai.get("model", "gpt-4o-mini"))),
    )

    if "CFM_AI_ACTIVATION_THRESHOLD" in os.environ:
        ai["activation_threshold"] = float(os.environ["CFM_AI_ACTIVATION_THRESHOLD"])
    if "CFM_AI_CACHE_TTL_SECONDS" in os.environ:
        ai["cache_ttl_seconds"] = int(os.environ["CFM_AI_CACHE_TTL_SECONDS"])
    if "CFM_AI_RETRY_COOLDOWN_SECONDS" in os.environ:
        ai["retry_cooldown_seconds"] = int(os.environ["CFM_AI_RETRY_COOLDOWN_SECONDS"])
    if "CFM_AI_MAX_TOKENS" in os.environ:
        ai["max_tokens"] = int(os.environ["CFM_AI_MAX_TOKENS"])

    timeout_seconds = os.environ.get("CFM_AI_REQUEST_TIMEOUT_SECONDS")
    if timeout_seconds is not None:
        ai["request_timeout_seconds"] = int(timeout_seconds)
    elif "API_TIMEOUT_MS" in os.environ:
        ai["request_timeout_seconds"] = max(
            5,
            min(30, int(int(os.environ["API_TIMEOUT_MS"]) / 1000)),
        )

    config["telegram"] = telegram
    config["dashboard"] = dashboard
    config["ai"] = ai
    config["auth"] = auth
    config["microstructure"] = microstructure
    config["telegram_bot"] = telegram_bot
    return config


def _normalized_exchange(config: dict) -> str:
    return str(config.get("exchange", "binance_usdm")).strip().lower()


def _is_okx_exchange(exchange: str) -> bool:
    return exchange in {"okx", "okx_swap", "okx_usdt_swap"}


def _normalized_data_source(exchange: str, data_source: str) -> str:
    source = str(data_source or "").strip().lower()
    if _is_okx_exchange(exchange):
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


def _source_label(exchange: str, data_source: str) -> str:
    venue = "OKX" if _is_okx_exchange(exchange) else "Binance"
    transport = "REST" if data_source == "rest" else "WebSocket"
    return f"{venue} {transport}"


def _build_source_specs(config: dict, exchange: str, data_source: str) -> list[SourceSpec]:
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
        item_source = _normalized_data_source(
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
        data_source=_normalized_data_source(exchange, data_source),
    )
    if primary not in specs:
        specs.insert(0, primary)
    return specs


def _build_source_context(config: dict, symbols: list[str], spec: SourceSpec) -> SourceContext:
    microstructure_config = config.get("microstructure", {})
    microstructure_state = MarketMicrostructureState(
        symbols,
        liquidations_enabled=(
            bool(microstructure_config.get("enabled", True))
            if _is_okx_exchange(spec.exchange)
            else True
        ),
        liquidation_feed_mode="poll" if _is_okx_exchange(spec.exchange) else "stream",
        liquidation_retention_seconds=int(
            microstructure_config.get("liquidation_retention_seconds", 86400)
        ),
    )
    microstructure_stream = None
    if microstructure_config.get("enabled", True) and not _is_okx_exchange(spec.exchange):
        microstructure_stream = BinanceFuturesMicrostructureStream(
            symbols,
            depth_levels=int(microstructure_config.get("depth_levels", 10)),
            depth_interval=str(microstructure_config.get("depth_interval", "500ms")),
        )

    if _is_okx_exchange(spec.exchange):
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
        on_switch=None,
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
        return self._active_context.exchange if self._active_context else self.specs[self._active_index].exchange

    @property
    def active_data_source(self) -> str:
        return self._active_context.data_source if self._active_context else self.specs[self._active_index].data_source

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
        self._active_context = _build_source_context(self.config, self.symbols, self.specs[index])
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
            _source_label(
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
        current_label = _source_label(self.active_exchange, self.active_data_source)
        next_spec = self.specs[next_index]
        next_label = _source_label(next_spec.exchange, next_spec.data_source)
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


async def run(config: dict) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    users_config = config.get("users", {})
    user_store = None
    if users_config.get("enabled", True):
        user_store = UserConfigStore(
            str(users_config.get("path", "data/user_configs.json")),
            config,
        )
    auth_manager = AuthManager(config.get("auth", {}))

    runtime_symbols = user_store.all_symbols() if user_store else config["symbols"]
    default_alert_score = float(config.get("thresholds", {}).get("anomaly_score", 70))

    detector = AnomalyDetector(
        symbols=runtime_symbols,
        window_seconds=int(config.get("window_seconds", 300)),
        warmup_seconds=int(config.get("warmup_seconds", 60)),
        alert_cooldown_seconds=int(config.get("alert_cooldown_seconds", 120)),
        thresholds=config.get("thresholds", {}),
        symbol_thresholds=(
            user_store.aggregate_symbol_thresholds()
            if user_store
            else config.get("symbol_thresholds", {})
        ),
    )
    alert = ConsoleAlert()
    telegram_config = config.get("telegram", {})
    if user_store:
        telegram_alert = TelegramAlert(
            enabled=True,
            users=user_store.aggregate_telegram_users(default_alert_score),
            cooldown_seconds=float(config.get("alert_cooldown_seconds", 120)),
        )
    else:
        telegram_alert = TelegramAlert(
            enabled=bool(telegram_config.get("enabled", False)),
            bot_token=str(telegram_config.get("bot_token", "")),
            chat_ids=telegram_config.get("chat_ids", []),
            users=telegram_config.get("users", []),
            cooldown_seconds=float(config.get("alert_cooldown_seconds", 120)),
        )
    ai_analyzer = AIAnalyzer(config.get("ai", {}))
    analysis_tasks: dict[tuple[int, str], asyncio.Task] = {}
    notification_tasks: dict[str, asyncio.Task] = {}
    runtime_tasks: set[asyncio.Task] = set()

    def track_task(task: asyncio.Task) -> asyncio.Task:
        runtime_tasks.add(task)
        task.add_done_callback(lambda done: runtime_tasks.discard(done))
        return task

    def clone_payload(payload: dict | None) -> dict | None:
        return deepcopy(payload) if payload else None

    def enrich_snapshot_with_ai(snapshot_data: dict, analysis: str | None) -> dict:
        payload = clone_payload(snapshot_data) or {}
        payload["ai_analysis"] = analysis or ""
        payload["ai_summary"] = summarize_analysis(analysis) if analysis else []
        return payload

    def enrich_event_with_ai(event: AnomalyEvent, analysis: str | None) -> AnomalyEvent:
        if not analysis:
            return event
        return replace(
            event,
            ai_analysis=analysis,
            ai_summary=tuple(summarize_analysis(analysis)),
        )

    def alert_ai_analyzer_candidates(snapshot_data: dict) -> list[AIAnalyzer]:
        symbol = str(snapshot_data.get("symbol", "")).upper()
        candidates: list[AIAnalyzer] = []
        seen: set[int] = set()

        def append(analyzer: AIAnalyzer | None) -> None:
            if not analyzer or not analyzer.enabled or not analyzer.api_key:
                return
            marker = id(analyzer)
            if marker in seen:
                return
            candidates.append(analyzer)
            seen.add(marker)

        if user_store and dashboard:
            for user in telegram_alert.users:
                owner_id = str(user.get("owner_id", ""))
                if not owner_id or not TelegramAlert._user_wants_snapshot(user, snapshot_data):
                    continue
                append(dashboard._get_ai_analyzer(owner_id))
            for owner_id, user_config in user_store.all().items():
                symbols = {str(item).upper() for item in user_config.get("symbols", [])}
                if symbol and symbols and symbol not in symbols:
                    continue
                append(dashboard._get_ai_analyzer(owner_id))
        else:
            append(ai_analyzer)

        return candidates

    def has_alert_ai(snapshot_data: dict | None) -> bool:
        return bool(snapshot_data and alert_ai_analyzer_candidates(snapshot_data))

    async def ensure_ai_analysis(
        symbol: str,
        snapshot_data: dict,
        force: bool = False,
        analyzer: AIAnalyzer | None = None,
        period: str | None = None,
    ) -> str | None:
        analyzer = analyzer or ai_analyzer
        if not analyzer.enabled or not analyzer.api_key or not snapshot_data:
            return None
        cached = analyzer.get_cached(symbol, period=period)
        if cached:
            return cached
        symbol = symbol.upper()
        task_key = (id(analyzer), f"{symbol}::{period or ''}")
        existing = analysis_tasks.get(task_key)
        if existing and not existing.done():
            return await existing
        task = track_task(
            asyncio.create_task(
                analyzer.analyze(symbol, snapshot_data, period=period, force=force)
            )
        )
        analysis_tasks[task_key] = task

        def cleanup(done: asyncio.Task, tracked_key: tuple[int, str] = task_key, tracked_task: asyncio.Task = task) -> None:
            if analysis_tasks.get(tracked_key) is tracked_task:
                analysis_tasks.pop(tracked_key, None)

        task.add_done_callback(cleanup)
        return await task

    async def get_alert_ai_analysis(symbol: str, snapshot_data: dict) -> str | None:
        for analyzer in alert_ai_analyzer_candidates(snapshot_data):
            timeout_seconds = max(3.0, min(float(analyzer.request_timeout), 12.0))
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(
                        ensure_ai_analysis(
                            symbol,
                            snapshot_data,
                            force=True,
                            analyzer=analyzer,
                            period="alert",
                        )
                    ),
                    timeout=timeout_seconds,
                )
                if result:
                    return result
            except asyncio.TimeoutError:
                logging.warning("AI analysis timeout for %s during alert enrichment", symbol)
                cached = analyzer.get_cached(symbol, period="alert")
                if cached:
                    return cached
        return None

    async def dispatch_event(event: AnomalyEvent, snapshot_data: dict | None) -> None:
        enriched_event = event
        if snapshot_data:
            analysis = await get_alert_ai_analysis(event.symbol, snapshot_data)
            enriched_event = enrich_event_with_ai(event, analysis)
        alert.send(enriched_event)
        if store:
            store.record_event(enriched_event)
            dashboard_state.set_events(store.recent(50))
        else:
            dashboard_state.add_event(enriched_event)

    def queue_snapshot_notification(snapshot_data: dict) -> None:
        symbol = str(snapshot_data.get("symbol", "")).upper()
        if not symbol:
            return
        existing = notification_tasks.get(symbol)
        if existing and not existing.done():
            return

        async def runner() -> None:
            analysis = await get_alert_ai_analysis(symbol, snapshot_data)
            telegram_alert.send_snapshot(enrich_snapshot_with_ai(snapshot_data, analysis))

        task = track_task(asyncio.create_task(runner()))
        notification_tasks[symbol] = task

        def cleanup(done: asyncio.Task, tracked_symbol: str = symbol, tracked_task: asyncio.Task = task) -> None:
            if notification_tasks.get(tracked_symbol) is tracked_task:
                notification_tasks.pop(tracked_symbol, None)

        task.add_done_callback(cleanup)

    followup_timeframes = TimeframeAnalysisService(cache_ttl_seconds=0)

    def resolve_alert_followup(request: dict) -> dict | None:
        return followup_timeframes.resolve_followup(
            symbol=str(request.get("symbol", "")),
            exchange=source_manager.active_exchange,
            horizon_minutes=int(request.get("horizon_minutes") or 0),
            event_time=float(request.get("event_time") or 0),
            target_time=float(request.get("target_time") or 0),
            anchor_price=float(request.get("anchor_price") or 0),
        )

    store = None
    storage_config = config.get("storage", {})
    if storage_config.get("enabled", True):
        store = AlertStore(
            str(storage_config.get("path", "data/monitor.db")),
            snapshot_interval_seconds=int(storage_config.get("snapshot_interval_seconds", 60)),
            followup_resolver=resolve_alert_followup,
        )

    exchange = _normalized_exchange(config)
    configured_data_source = str(config.get("data_source", "auto")).lower()
    data_source = _normalized_data_source(exchange, configured_data_source)
    source_specs = _build_source_specs(config, exchange, data_source)
    dashboard_state = DashboardState(
        runtime_symbols,
        data_source=source_specs[0].data_source,
        exchange=source_specs[0].exchange,
    )

    failover_config = config.get("failover", {})
    def handle_source_switch(active_exchange: str, active_data_source: str, note: str) -> None:
        detector.reset_windows(source_manager.symbols)
        dashboard_state.set_source(
            exchange=active_exchange,
            data_source=active_data_source,
            note=note,
        )

    source_manager = SourceFailoverManager(
        config=config,
        specs=source_specs,
        symbols=runtime_symbols,
        stale_after_seconds=float(failover_config.get("stale_after_seconds", 20)),
        switch_cooldown_seconds=float(
            failover_config.get("switch_cooldown_seconds", 45)
        ),
        on_switch=handle_source_switch,
    )

    dashboard = None
    dashboard_config = config.get("dashboard", {})
    if dashboard_config.get("enabled", True):
        def apply_user_runtime_config() -> None:
            if not user_store:
                return
            symbols = user_store.all_symbols()
            detector.set_symbols(symbols)
            detector.symbol_thresholds = user_store.aggregate_symbol_thresholds()
            detector.reset_windows(symbols)
            source_manager.set_symbols(symbols)
            dashboard_state.set_symbols(symbols)
            telegram_alert.set_config(
                True,
                users=user_store.aggregate_telegram_users(default_alert_score),
            )

        def update_symbols(symbols: list[str]) -> None:
            config["symbols"] = symbols
            detector.set_symbols(symbols)
            detector.reset_windows(symbols)
            source_manager.set_symbols(symbols)
            dashboard_state.set_symbols(symbols)
            save_config(Path(config.get("_config_path", "config.yaml")), config)

        dashboard = DashboardServer(
            state=dashboard_state,
            host=dashboard_config.get("host", "127.0.0.1"),
            port=int(dashboard_config.get("port", 8765)),
            on_symbols_change=update_symbols,
            telegram_alert=telegram_alert,
            detector=detector,
            ai_analyzer=ai_analyzer,
            config=config,
            config_path=Path(config.get("_config_path", "config.yaml")),
            user_config_store=user_store,
            on_user_config_change=apply_user_runtime_config,
            auth_manager=auth_manager,
            period_liquidation_provider=source_manager.liquidation_summary,
        )
        dashboard.start()
        dashboard.set_event_loop(asyncio.get_running_loop())
        if store:
            dashboard_state.set_events(store.recent(50))

    logging.info(
        "Monitoring %s on %s via %s",
        ", ".join(runtime_symbols),
        source_specs[0].exchange,
        source_specs[0].data_source,
    )

    background_tasks = []
    telegram_bot_config = config.get("telegram_bot", {})
    if telegram_bot_config.get("enabled", True):
        def telegram_bot_users() -> list[dict]:
            if user_store:
                return user_store.aggregate_telegram_users(default_alert_score)
            return telegram_alert.users

        def telegram_bot_ai_analyzer(owner_id: str) -> AIAnalyzer | None:
            if user_store and dashboard:
                return dashboard._get_ai_analyzer(owner_id)
            return ai_analyzer

        telegram_bot_responder = TelegramBotResponder(
            enabled=True,
            get_users=telegram_bot_users,
            get_snapshot=dashboard_state.get_symbol_data,
            get_ai_analyzer=telegram_bot_ai_analyzer,
            poll_interval_seconds=float(telegram_bot_config.get("poll_interval_seconds", 2)),
            request_timeout_seconds=int(telegram_bot_config.get("request_timeout_seconds", 20)),
            ai_cooldown_seconds=int(telegram_bot_config.get("ai_cooldown_seconds", 20)),
        )
        background_tasks.append(asyncio.create_task(telegram_bot_responder.run()))

    try:
        async for trade in source_manager.listen():
            event = detector.update(trade)
            snapshot = detector.snapshot(trade["symbol"])
            snapshot_payload = None
            if snapshot:
                dashboard_state.update_snapshot(snapshot)
                snapshot_data = dashboard_state.get_symbol_data(snapshot.symbol)
                if snapshot_data:
                    snapshot_payload = clone_payload(snapshot_data)
                    if snapshot_payload and telegram_alert.has_ready_targets(snapshot_payload):
                        if has_alert_ai(snapshot_payload):
                            queue_snapshot_notification(snapshot_payload)
                        else:
                            telegram_alert.send_snapshot(snapshot_payload)
                if store:
                    followups_updated = store.record_snapshot(snapshot)
                    if followups_updated:
                        dashboard_state.set_events(store.recent(50))
                if (
                    not user_store
                    and ai_analyzer.enabled
                    and bool(config.get("ai", {}).get("auto_analyze_enabled", False))
                ):
                    if snapshot_payload and ai_analyzer.should_activate(snapshot_payload):
                        track_task(
                            asyncio.create_task(ai_analyzer.analyze(snapshot.symbol, snapshot_payload))
                        )
            if event:
                if snapshot_payload and has_alert_ai(snapshot_payload):
                    track_task(asyncio.create_task(dispatch_event(event, snapshot_payload)))
                else:
                    alert.send(event)
                    if store:
                        store.record_event(event)
                        dashboard_state.set_events(store.recent(50))
                    else:
                        dashboard_state.add_event(event)
    finally:
        await source_manager.close()
        active_tasks = [task for task in background_tasks if not task.done()]
        active_tasks.extend(task for task in runtime_tasks if not task.done())
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance USDT futures anomaly monitor")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file. Default: config.yaml",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)
    config = apply_env_overrides(config)
    config["_config_path"] = str(config_path)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
