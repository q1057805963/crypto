import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml

from monitor.ai_analysis import AIAnalyzer
from monitor.alert import ConsoleAlert
from monitor.anomaly import AnomalyDetector
from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.dashboard import DashboardServer, DashboardState
from monitor.microstructure import BinanceFuturesMicrostructureStream, MarketMicrostructureState
from monitor.storage import AlertStore
from monitor.telegram import TelegramAlert


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def save_config(path: Path, config: dict) -> None:
    public_config = {key: value for key, value in config.items() if not key.startswith("_")}
    if config.get("_ai_api_key_from_env"):
        ai = dict(public_config.get("ai", {}))
        ai["api_key"] = ""
        public_config["ai"] = ai
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(public_config, file, allow_unicode=True, sort_keys=False)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _migrate_telegram_config(telegram: dict) -> dict:
    """Migrate legacy chat_id (string) to chat_ids (list)."""
    if "chat_id" in telegram and "chat_ids" not in telegram:
        old = str(telegram.pop("chat_id", ""))
        telegram["chat_ids"] = [old] if old.strip() else []
    elif "chat_id" in telegram:
        telegram.pop("chat_id", None)
    if "chat_ids" not in telegram:
        telegram["chat_ids"] = []
    return telegram


def apply_env_overrides(config: dict) -> dict:
    config = dict(config)
    telegram = dict(config.get("telegram", {}))
    dashboard = dict(config.get("dashboard", {}))
    ai = dict(config.get("ai", {}))

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

    ai_key = os.environ.get("CFM_AI_API_KEY")
    if ai_key is None:
        ai_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if ai_key is not None:
        ai["api_key"] = ai_key
        config["_ai_api_key_from_env"] = True

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
    return config


async def run(config: dict) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    detector = AnomalyDetector(
        symbols=config["symbols"],
        window_seconds=int(config.get("window_seconds", 300)),
        warmup_seconds=int(config.get("warmup_seconds", 60)),
        alert_cooldown_seconds=int(config.get("alert_cooldown_seconds", 120)),
        thresholds=config.get("thresholds", {}),
        symbol_thresholds=config.get("symbol_thresholds", {}),
    )
    alert = ConsoleAlert()
    telegram_config = config.get("telegram", {})
    telegram_alert = TelegramAlert(
        enabled=bool(telegram_config.get("enabled", False)),
        bot_token=str(telegram_config.get("bot_token", "")),
        chat_ids=telegram_config.get("chat_ids", []),
    )
    ai_analyzer = AIAnalyzer(config.get("ai", {}))
    store = None
    storage_config = config.get("storage", {})
    if storage_config.get("enabled", True):
        store = AlertStore(
            str(storage_config.get("path", "data/monitor.db")),
            snapshot_interval_seconds=int(storage_config.get("snapshot_interval_seconds", 60)),
        )

    microstructure_config = config.get("microstructure", {})
    microstructure_state = MarketMicrostructureState(config["symbols"])
    microstructure_stream = None
    if microstructure_config.get("enabled", True):
        microstructure_stream = BinanceFuturesMicrostructureStream(
            config["symbols"],
            depth_levels=int(microstructure_config.get("depth_levels", 10)),
            depth_interval=str(microstructure_config.get("depth_interval", "500ms")),
        )

    if config.get("data_source", "rest") == "websocket":
        stream = BinanceFuturesAggTradeStream(
            config["symbols"],
            microstructure_state=microstructure_state,
        )
    else:
        stream = BinanceFuturesTickerPoller(
            config["symbols"],
            poll_interval_seconds=float(config.get("rest_poll_interval_seconds", 2)),
            per_symbol_delay_ms=int(config.get("rest_per_symbol_delay_ms", 150)),
            oi_poll_interval_seconds=float(config.get("oi_poll_interval_seconds", 30)),
            funding_poll_interval_seconds=float(
                config.get("funding_poll_interval_seconds", 60)
            ),
            microstructure_state=microstructure_state,
        )
    dashboard_state = DashboardState(
        config["symbols"],
        data_source=config.get("data_source", "rest"),
    )

    dashboard_config = config.get("dashboard", {})
    if dashboard_config.get("enabled", True):
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
        )
        dashboard.start()
        dashboard.set_event_loop(asyncio.get_running_loop())
        if store:
            dashboard_state.set_events(store.recent(50))

    logging.info(
        "Monitoring %s via %s",
        ", ".join(config["symbols"]),
        config.get("data_source", "rest"),
    )

    background_tasks = []
    if microstructure_stream:
        background_tasks.append(asyncio.create_task(microstructure_stream.run(microstructure_state)))

    try:
        async for trade in stream.listen():
            event = detector.update(trade)
            snapshot = detector.snapshot(trade["symbol"])
            if snapshot:
                dashboard_state.update_snapshot(snapshot)
                if store:
                    store.record_snapshot(snapshot)
                if ai_analyzer.enabled:
                    score = float(snapshot.score)
                    if score >= ai_analyzer.activation_threshold:
                        asyncio.ensure_future(ai_analyzer.analyze(
                            snapshot.symbol, dashboard_state.get_symbol_data(snapshot.symbol)
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
