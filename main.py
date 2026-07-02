import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml

from monitor.alert import ConsoleAlert
from monitor.anomaly import AnomalyDetector
from monitor.binance_rest import BinanceFuturesTickerPoller
from monitor.binance_ws import BinanceFuturesAggTradeStream
from monitor.dashboard import DashboardServer, DashboardState
from monitor.storage import AlertStore
from monitor.telegram import TelegramAlert


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def save_config(path: Path, config: dict) -> None:
    public_config = {key: value for key, value in config.items() if not key.startswith("_")}
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(public_config, file, allow_unicode=True, sort_keys=False)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def apply_env_overrides(config: dict) -> dict:
    config = dict(config)
    telegram = dict(config.get("telegram", {}))
    dashboard = dict(config.get("dashboard", {}))

    telegram["enabled"] = _env_flag(
        "CFM_TELEGRAM_ENABLED",
        bool(telegram.get("enabled", False)),
    )
    telegram["bot_token"] = os.environ.get(
        "CFM_TELEGRAM_BOT_TOKEN",
        str(telegram.get("bot_token", "")),
    )
    telegram["chat_id"] = os.environ.get(
        "CFM_TELEGRAM_CHAT_ID",
        str(telegram.get("chat_id", "")),
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

    config["telegram"] = telegram
    config["dashboard"] = dashboard
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
    )
    alert = ConsoleAlert()
    telegram_config = config.get("telegram", {})
    telegram_alert = TelegramAlert(
        enabled=bool(telegram_config.get("enabled", False)),
        bot_token=str(telegram_config.get("bot_token", "")),
        chat_id=str(telegram_config.get("chat_id", "")),
    )
    store = None
    storage_config = config.get("storage", {})
    if storage_config.get("enabled", True):
        store = AlertStore(str(storage_config.get("path", "data/monitor.db")))

    if config.get("data_source", "rest") == "websocket":
        stream = BinanceFuturesAggTradeStream(config["symbols"])
    else:
        stream = BinanceFuturesTickerPoller(
            config["symbols"],
            poll_interval_seconds=float(config.get("rest_poll_interval_seconds", 2)),
            per_symbol_delay_ms=int(config.get("rest_per_symbol_delay_ms", 150)),
            oi_poll_interval_seconds=float(config.get("oi_poll_interval_seconds", 30)),
            funding_poll_interval_seconds=float(
                config.get("funding_poll_interval_seconds", 60)
            ),
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
            dashboard_state.set_symbols(symbols)
            save_config(Path(config.get("_config_path", "config.yaml")), config)

        dashboard = DashboardServer(
            state=dashboard_state,
            host=dashboard_config.get("host", "127.0.0.1"),
            port=int(dashboard_config.get("port", 8765)),
            on_symbols_change=update_symbols,
        )
        dashboard.start()
        if store:
            dashboard_state.set_events(store.recent(50))

    logging.info(
        "Monitoring %s via %s",
        ", ".join(config["symbols"]),
        config.get("data_source", "rest"),
    )
    async for trade in stream.listen():
        event = detector.update(trade)
        snapshot = detector.snapshot(trade["symbol"])
        if snapshot:
            dashboard_state.update_snapshot(snapshot)
        if event:
            alert.send(event)
            telegram_alert.send(event)
            if store:
                store.record_event(event)
            dashboard_state.add_event(event)


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
