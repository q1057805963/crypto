import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml

from monitor.auth import AuthManager
from monitor.ai_analysis import AIAnalyzer
from monitor.alert import ConsoleAlert
from monitor.anomaly import AnomalyDetector
from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.dashboard import DashboardServer, DashboardState
from monitor.microstructure import BinanceFuturesMicrostructureStream, MarketMicrostructureState
from monitor.okx_rest import OkxSwapTickerPoller
from monitor.storage import AlertStore
from monitor.telegram import TelegramAlert, normalize_telegram_users
from monitor.telegram_bot import TelegramBotResponder
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
        )
    else:
        telegram_alert = TelegramAlert(
            enabled=bool(telegram_config.get("enabled", False)),
            bot_token=str(telegram_config.get("bot_token", "")),
            chat_ids=telegram_config.get("chat_ids", []),
            users=telegram_config.get("users", []),
        )
    ai_analyzer = AIAnalyzer(config.get("ai", {}))
    store = None
    storage_config = config.get("storage", {})
    if storage_config.get("enabled", True):
        store = AlertStore(
            str(storage_config.get("path", "data/monitor.db")),
            snapshot_interval_seconds=int(storage_config.get("snapshot_interval_seconds", 60)),
        )

    exchange = _normalized_exchange(config)
    configured_data_source = str(config.get("data_source", "rest")).lower()
    data_source = configured_data_source
    if _is_okx_exchange(exchange) and configured_data_source != "rest":
        logging.warning("OKX exchange currently uses REST polling; ignoring data_source=%s", configured_data_source)
        data_source = "rest"

    microstructure_config = config.get("microstructure", {})
    microstructure_state = MarketMicrostructureState(
        runtime_symbols,
        liquidations_enabled=(
            bool(microstructure_config.get("enabled", True))
            if _is_okx_exchange(exchange)
            else True
        ),
        liquidation_feed_mode="poll" if _is_okx_exchange(exchange) else "stream",
    )
    microstructure_stream = None
    if microstructure_config.get("enabled", True) and not _is_okx_exchange(exchange):
        microstructure_stream = BinanceFuturesMicrostructureStream(
            runtime_symbols,
            depth_levels=int(microstructure_config.get("depth_levels", 10)),
            depth_interval=str(microstructure_config.get("depth_interval", "500ms")),
        )

    if _is_okx_exchange(exchange):
        stream = OkxSwapTickerPoller(
            runtime_symbols,
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
    elif data_source == "websocket":
        stream = BinanceFuturesAggTradeStream(
            runtime_symbols,
            microstructure_state=microstructure_state,
        )
    else:
        stream = BinanceFuturesTickerPoller(
            runtime_symbols,
            poll_interval_seconds=float(config.get("rest_poll_interval_seconds", 2)),
            per_symbol_delay_ms=int(config.get("rest_per_symbol_delay_ms", 150)),
            oi_poll_interval_seconds=float(config.get("oi_poll_interval_seconds", 30)),
            funding_poll_interval_seconds=float(
                config.get("funding_poll_interval_seconds", 60)
            ),
            microstructure_state=microstructure_state,
        )
    dashboard_state = DashboardState(
        runtime_symbols,
        data_source=data_source,
        exchange=exchange,
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
            stream.set_symbols(symbols)
            microstructure_state.set_symbols(symbols)
            if microstructure_stream:
                microstructure_stream.set_symbols(symbols)
            dashboard_state.set_symbols(symbols)
            telegram_alert.set_config(
                True,
                users=user_store.aggregate_telegram_users(default_alert_score),
            )

        def update_symbols(symbols: list[str]) -> None:
            config["symbols"] = symbols
            detector.set_symbols(symbols)
            stream.set_symbols(symbols)
            microstructure_state.set_symbols(symbols)
            if microstructure_stream:
                microstructure_stream.set_symbols(symbols)
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
        )
        dashboard.start()
        dashboard.set_event_loop(asyncio.get_running_loop())
        if store:
            dashboard_state.set_events(store.recent(50))

    logging.info(
        "Monitoring %s on %s via %s",
        ", ".join(runtime_symbols),
        exchange,
        data_source,
    )

    background_tasks = []
    if microstructure_stream:
        background_tasks.append(asyncio.create_task(microstructure_stream.run(microstructure_state)))

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
        async for trade in stream.listen():
            event = detector.update(trade)
            snapshot = detector.snapshot(trade["symbol"])
            if snapshot:
                dashboard_state.update_snapshot(snapshot)
                if store:
                    store.record_snapshot(snapshot)
                if (
                    not user_store
                    and ai_analyzer.enabled
                    and bool(config.get("ai", {}).get("auto_analyze_enabled", False))
                ):
                    snapshot_data = dashboard_state.get_symbol_data(snapshot.symbol)
                    if snapshot_data and ai_analyzer.should_activate(snapshot_data):
                        asyncio.ensure_future(ai_analyzer.analyze(
                            snapshot.symbol, snapshot_data
                        ))
            if event:
                alert.send(event)
                telegram_alert.send(event)
                if store:
                    store.record_event(event)
                dashboard_state.add_event(event)
    finally:
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


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
