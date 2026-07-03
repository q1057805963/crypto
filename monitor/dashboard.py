import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time

from monitor.ai_analysis import AIAnalyzer
from monitor.auth import AuthError, AuthManager
from monitor.anomaly import AnomalyEvent, SymbolSnapshot
from monitor.rules import enabled_trigger_count
from monitor.telegram import normalize_telegram_users, send_text_to_telegram_users
from monitor.user_config import UserConfigStore


def normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw_symbol in symbols:
        symbol = "".join(ch for ch in raw_symbol.upper().strip() if ch.isalnum())
        if not symbol:
            continue
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def _masked(value: str) -> bool:
    return bool(value) and ("***" in value or set(value) == {"*"})


def telegram_users_response(telegram: dict) -> list[dict]:
    users = normalize_telegram_users(
        telegram.get("users"),
        str(telegram.get("bot_token", "")),
        telegram.get("chat_ids", []),
    )
    return [
        {
            "name": user.get("name", ""),
            "enabled": bool(user.get("enabled", True)),
            "bot_token": "********" if user.get("bot_token") else "",
            "bot_token_set": bool(user.get("bot_token")),
            "chat_ids": user.get("chat_ids", []),
        }
        for user in users
    ]


def merge_telegram_users(existing_users: list[dict], incoming_users: list[dict]) -> list[dict]:
    existing = normalize_telegram_users(existing_users)
    merged = []
    for index, raw_user in enumerate(incoming_users):
        previous = existing[index] if index < len(existing) else {}
        token = str(raw_user.get("bot_token", ""))
        if not token or _masked(token):
            token = str(previous.get("bot_token", ""))
        chat_ids = [
            str(chat_id).strip()
            for chat_id in raw_user.get("chat_ids", [])
            if str(chat_id).strip()
        ]
        merged.append(
            {
                "name": str(raw_user.get("name") or previous.get("name") or f"用户{index + 1}").strip(),
                "enabled": bool(raw_user.get("enabled", True)),
                "bot_token": token,
                "chat_ids": chat_ids,
            }
        )
    return merged


class DashboardState:
    def __init__(self, symbols: list[str], data_source: str, exchange: str = "binance_usdm") -> None:
        self._lock = threading.Lock()
        self._data_source = data_source
        self._exchange = exchange
        self._source_note = ""
        self._symbols = {
            symbol: {
                "symbol": symbol,
                "score": 0,
                "direction": "waiting",
                "price": None,
                "updated_at": None,
                "price_move_pct_1m": 0,
                "price_move_pct_5m": 0,
                "quote_volume_1m": 0,
                "volume_multiplier": 0,
                "taker_buy_ratio_1m": 0.5,
                "trade_count_1m": 0,
                "open_interest": 0,
                "oi_change_pct_5m": 0,
                "funding_rate": 0,
                "spread_bps": 0,
                "depth_imbalance": 0,
                "bid_depth_notional": 0,
                "ask_depth_notional": 0,
                "depth_drop_pct_1m": 0,
                "support_price": 0,
                "resistance_price": 0,
                "support_distance_pct": 0,
                "resistance_distance_pct": 0,
                "window_vwap": 0,
                "vwap_deviation_pct": 0,
                "range_position_pct": 50,
                "bid_wall_price": 0,
                "bid_wall_notional": 0,
                "ask_wall_price": 0,
                "ask_wall_notional": 0,
                "long_liquidation_quote_1m": 0,
                "short_liquidation_quote_1m": 0,
                "liquidation_total_quote_1m": 0,
                "liquidation_event_count_1m": 0,
                "liquidation_data_status": "unavailable",
                "microstructure_status": "unavailable",
                "depth_data_age_seconds": None,
                "last_liquidation_age_seconds": None,
                "price_series_5m": [],
                "volume_series_5m": [],
                "oi_series_5m": [],
                "risk_level": "低风险",
                "bias": "观察：暂无明确方向",
                "confidence": 0,
                "reasons": [],
                "suggestions": [],
            }
            for symbol in normalize_symbols(symbols)
        }
        self._events: list[dict] = []

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            normalized = normalize_symbols(symbols)
            self._symbols = {
                symbol: self._symbols.get(
                    symbol,
                    {
                        "symbol": symbol,
                        "score": 0,
                        "direction": "waiting",
                        "price": None,
                        "updated_at": None,
                        "price_move_pct_1m": 0,
                        "price_move_pct_5m": 0,
                        "quote_volume_1m": 0,
                        "volume_multiplier": 0,
                        "taker_buy_ratio_1m": 0.5,
                        "trade_count_1m": 0,
                        "open_interest": 0,
                        "oi_change_pct_5m": 0,
                        "funding_rate": 0,
                        "spread_bps": 0,
                        "depth_imbalance": 0,
                        "bid_depth_notional": 0,
                        "ask_depth_notional": 0,
                        "depth_drop_pct_1m": 0,
                        "support_price": 0,
                        "resistance_price": 0,
                        "support_distance_pct": 0,
                        "resistance_distance_pct": 0,
                        "window_vwap": 0,
                        "vwap_deviation_pct": 0,
                        "range_position_pct": 50,
                        "bid_wall_price": 0,
                        "bid_wall_notional": 0,
                        "ask_wall_price": 0,
                        "ask_wall_notional": 0,
                        "long_liquidation_quote_1m": 0,
                        "short_liquidation_quote_1m": 0,
                        "liquidation_total_quote_1m": 0,
                        "liquidation_event_count_1m": 0,
                        "liquidation_data_status": "unavailable",
                        "microstructure_status": "unavailable",
                        "depth_data_age_seconds": None,
                        "last_liquidation_age_seconds": None,
                        "price_series_5m": [],
                        "volume_series_5m": [],
                        "oi_series_5m": [],
                        "risk_level": "低风险",
                        "bias": "观察：暂无明确方向",
                        "confidence": 0,
                        "reasons": [],
                        "suggestions": [],
                    },
                )
                for symbol in normalized
            }

    def update_snapshot(self, snapshot: SymbolSnapshot) -> None:
        with self._lock:
            data = asdict(snapshot)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            self._symbols[snapshot.symbol] = data

    def set_events(self, events: list[dict]) -> None:
        with self._lock:
            self._events = events[:50]

    def add_event(self, event: AnomalyEvent) -> None:
        with self._lock:
            data = asdict(event)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            data["ai_summary"] = list(data.get("ai_summary", ()))
            data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._events.insert(0, data)
            self._events = self._events[:50]

    def get_symbol_data(self, symbol: str) -> dict | None:
        with self._lock:
            return self._symbols.get(symbol.upper())

    def set_source(self, *, exchange: str, data_source: str, note: str = "") -> None:
        with self._lock:
            self._exchange = exchange
            self._data_source = data_source
            self._source_note = note

    def clear_source_note(self) -> None:
        with self._lock:
            self._source_note = ""

    def as_payload(self, symbols_filter: list[str] | None = None) -> dict:
        with self._lock:
            symbols = list(self._symbols.values())
            if symbols_filter is not None:
                wanted = {symbol.upper() for symbol in symbols_filter}
                symbols = [symbol for symbol in symbols if symbol["symbol"].upper() in wanted]
            symbols.sort(key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
            events = list(self._events)
            if symbols_filter is not None:
                wanted = {symbol.upper() for symbol in symbols_filter}
                events = [event for event in events if str(event.get("symbol", "")).upper() in wanted]
            return {
                "generated_at": time(),
                "data_source": self._data_source,
                "exchange": self._exchange,
                "source_note": self._source_note,
                "symbols": symbols,
                "events": events,
            }


class DashboardServer:
    def __init__(
        self,
        state: DashboardState,
        host: str,
        port: int,
        on_symbols_change,
        telegram_alert=None,
        detector=None,
        ai_analyzer=None,
        config: dict | None = None,
        config_path: str = "config.yaml",
        user_config_store: UserConfigStore | None = None,
        on_user_config_change=None,
        auth_manager: AuthManager | None = None,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.on_symbols_change = on_symbols_change
        self.telegram_alert = telegram_alert
        self.detector = detector
        self.ai_analyzer = ai_analyzer
        self.config = config or {}
        self.config_path = config_path
        self.user_config_store = user_config_store
        self.on_user_config_change = on_user_config_change
        self.auth_manager = auth_manager
        self._ai_analyzers: dict[str, AIAnalyzer] = {}
        self._event_loop = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._config_lock = threading.Lock()
        self._auth_attempt_lock = threading.Lock()
        self._auth_attempts: dict[str, list[float]] = {}

    def set_event_loop(self, loop) -> None:
        self._event_loop = loop

    def _save_config(self) -> None:
        from pathlib import Path
        public_config = {k: v for k, v in self.config.items() if not k.startswith("_")}
        if self.config.get("_ai_api_key_from_env") or self.config.get("_ai_api_key_runtime_only"):
            ai = dict(public_config.get("ai", {}))
            ai["api_key"] = ""
            public_config["ai"] = ai
        if self.config.get("_auth_secret_from_env"):
            auth = dict(public_config.get("auth", {}))
            auth["jwt_secret"] = ""
            public_config["auth"] = auth
        with open(Path(self.config_path), "w", encoding="utf-8") as f:
            import yaml
            yaml.safe_dump(public_config, f, allow_unicode=True, sort_keys=False)

    def _get_user_config(self, user_id: str) -> dict:
        if self.user_config_store:
            existed = self.user_config_store.has(user_id)
            user_config = self.user_config_store.get(user_id)
            if not existed:
                self._notify_user_config_change()
            return user_config
        return {
            "symbols": self.config.get("symbols", []),
            "telegram": self.config.get("telegram", {}),
            "ai": self.config.get("ai", {}),
            "symbol_thresholds": self.config.get("symbol_thresholds", {}),
        }

    def _get_ai_analyzer(self, user_id: str, ai_cfg: dict | None = None) -> AIAnalyzer | None:
        if not self.user_config_store:
            return self.ai_analyzer
        if user_id not in self._ai_analyzers:
            self._ai_analyzers[user_id] = AIAnalyzer(ai_cfg or self._get_user_config(user_id).get("ai", {}))
        elif ai_cfg is not None:
            self._ai_analyzers[user_id].update_config(ai_cfg)
        return self._ai_analyzers[user_id]

    def _notify_user_config_change(self) -> None:
        if self.on_user_config_change:
            self.on_user_config_change()

    def start(self) -> None:
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logging.info("Dashboard available at http://%s:%s", self.host, self.port)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self
        state = self.state
        on_symbols_change = self.on_symbols_change

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._request_user = None
                path = self.path.split("?")[0]

                if path == "/" or path.startswith("/index.html"):
                    self._send_text(INDEX_HTML, "text/html; charset=utf-8")
                    return

                if path == "/api/auth/status":
                    self._send_json(
                        server_ref.auth_manager.public_status()
                        if server_ref.auth_manager
                        else {"enabled": False, "has_users": False, "allow_registration": True}
                    )
                    return

                if path == "/api/auth/me":
                    user = self._require_user()
                    if not user:
                        return
                    self._send_json({"ok": True, "user": user})
                    return

                if path.startswith("/api/"):
                    user = self._require_user()
                    if not user:
                        return
                    self._request_user = user

                if path == "/api/state":
                    user_config = server_ref._get_user_config(self._user_id())
                    self._send_json(state.as_payload(user_config.get("symbols", [])))
                    return

                if path == "/api/telegram":
                    tg = server_ref._get_user_config(self._user_id()).get("telegram", {})
                    self._send_json({
                        "enabled": tg.get("enabled", False),
                        "users": telegram_users_response(tg),
                    })
                    return

                if path == "/api/symbol_thresholds":
                    user_config = server_ref._get_user_config(self._user_id())
                    self._send_json(
                        {
                            "default_score": server_ref.config.get("thresholds", {}).get(
                                "anomaly_score", 70
                            ),
                            "symbol_thresholds": user_config.get("symbol_thresholds", {}),
                        }
                    )
                    return

                if path == "/api/ai/config":
                    ai_cfg = dict(server_ref._get_user_config(self._user_id()).get("ai", {}))
                    if ai_cfg.get("api_key"):
                        ai_cfg["api_key"] = ai_cfg["api_key"][:8] + "***"
                    self._send_json(ai_cfg)
                    return

                if path == "/api/ai/analysis":
                    self._handle_ai_analysis_get()
                    return

                self.send_error(404)

            def do_POST(self) -> None:
                self._request_user = None
                path = self.path.split("?")[0]

                if path == "/api/auth/register":
                    self._handle_auth_register()
                    return

                if path == "/api/auth/login":
                    self._handle_auth_login()
                    return

                if path.startswith("/api/"):
                    user = self._require_user()
                    if not user:
                        return
                    self._request_user = user

                if path == "/api/symbols":
                    self._handle_symbols_post()
                    return

                if path == "/api/telegram/test":
                    self._handle_telegram_test_post()
                    return

                if path == "/api/telegram":
                    self._handle_telegram_post()
                    return

                if path == "/api/symbol_thresholds":
                    self._handle_symbol_thresholds_post()
                    return

                if path == "/api/ai/config":
                    self._handle_ai_config_post()
                    return

                self.send_error(404)

            def _handle_auth_register(self) -> None:
                try:
                    if self._auth_rate_limited("register"):
                        raise AuthError("尝试过于频繁，请稍后再试")
                    if not server_ref.auth_manager or not server_ref.auth_manager.enabled:
                        raise AuthError("认证未启用")
                    if not server_ref.auth_manager.can_register():
                        raise AuthError("注册已关闭")
                    payload = self._read_json()
                    user = server_ref.auth_manager.register(
                        str(payload.get("username", "")),
                        str(payload.get("password", "")),
                    )
                    if server_ref.user_config_store:
                        server_ref.user_config_store.get(user["user_id"])
                        server_ref._notify_user_config_change()
                    self._send_json({
                        "ok": True,
                        "user": user,
                        "token": server_ref.auth_manager.issue_token(user),
                    })
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_auth_login(self) -> None:
                try:
                    if self._auth_rate_limited("login"):
                        raise AuthError("尝试过于频繁，请稍后再试")
                    if not server_ref.auth_manager or not server_ref.auth_manager.enabled:
                        raise AuthError("认证未启用")
                    payload = self._read_json()
                    user = server_ref.auth_manager.login(
                        str(payload.get("username", "")),
                        str(payload.get("password", "")),
                    )
                    if server_ref.user_config_store:
                        server_ref.user_config_store.get(user["user_id"])
                        server_ref._notify_user_config_change()
                    self._send_json({
                        "ok": True,
                        "user": user,
                        "token": server_ref.auth_manager.issue_token(user),
                    })
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=401)

            def _handle_symbols_post(self) -> None:
                try:
                    payload = self._read_json()
                    symbols = normalize_symbols(payload.get("symbols", []))
                    if not symbols:
                        raise ValueError("symbols cannot be empty")
                    if server_ref.user_config_store:
                        server_ref.user_config_store.update_symbols(self._user_id(), symbols)
                        server_ref._notify_user_config_change()
                    else:
                        on_symbols_change(symbols)
                    self._send_json({"ok": True, "symbols": symbols})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_telegram_post(self) -> None:
                try:
                    payload = self._read_json()
                    with server_ref._config_lock:
                        if server_ref.user_config_store:
                            user_id = self._user_id()
                            tg = dict(server_ref._get_user_config(user_id).get("telegram", {}))
                        else:
                            user_id = ""
                            tg = dict(server_ref.config.get("telegram", {}))
                        if "enabled" in payload:
                            tg["enabled"] = bool(payload["enabled"])
                        if "bot_token" in payload:
                            bot_token = str(payload["bot_token"])
                            if not _masked(bot_token):
                                tg["bot_token"] = bot_token
                        if "chat_ids" in payload:
                            tg["chat_ids"] = [str(cid).strip() for cid in payload["chat_ids"] if str(cid).strip()]
                        if "users" in payload:
                            tg["users"] = merge_telegram_users(tg.get("users", []), payload["users"])
                        if server_ref.user_config_store:
                            server_ref.user_config_store.update_telegram(user_id, tg)
                            server_ref._notify_user_config_change()
                        else:
                            server_ref.config["telegram"] = tg
                            if server_ref.telegram_alert:
                                server_ref.telegram_alert.set_config(
                                    bool(tg.get("enabled", False)),
                                    str(tg.get("bot_token", "")),
                                    tg.get("chat_ids", []),
                                    tg.get("users", []),
                                )
                            server_ref._save_config()
                    self._send_json({"ok": True})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_telegram_test_post(self) -> None:
                try:
                    payload = self._read_json()
                    with server_ref._config_lock:
                        if server_ref.user_config_store:
                            user_id = self._user_id()
                            tg = dict(server_ref._get_user_config(user_id).get("telegram", {}))
                        else:
                            tg = dict(server_ref.config.get("telegram", {}))
                        if "enabled" in payload:
                            tg["enabled"] = bool(payload["enabled"])
                        if "users" in payload:
                            tg["users"] = merge_telegram_users(tg.get("users", []), payload["users"])
                        users = normalize_telegram_users(
                            tg.get("users"),
                            str(tg.get("bot_token", "")),
                            tg.get("chat_ids", []),
                        )
                    if not tg.get("enabled", False):
                        self._send_json({"ok": False, "error": "请先开启推送"}, status=400)
                        return
                    text = "Crypto Futures Monitor 测试推送：Telegram 绑定已连通。"
                    result = send_text_to_telegram_users(users, text)
                    if result["sent"] <= 0:
                        self._send_json(
                            {
                                "ok": False,
                                "error": "没有可用通道或发送失败",
                                "result": result,
                            },
                            status=400,
                        )
                        return
                    self._send_json({"ok": True, "result": result})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_symbol_thresholds_post(self) -> None:
                try:
                    payload = self._read_json()
                    symbol = str(payload.get("symbol", "")).upper()
                    if not symbol:
                        raise ValueError("symbol is required")
                    has_score = "anomaly_score" in payload
                    score = payload.get("anomaly_score")
                    has_push_rules = "push_rules" in payload
                    push_rules = payload.get("push_rules")
                    with server_ref._config_lock:
                        if server_ref.user_config_store:
                            user_id = self._user_id()
                            st = dict(server_ref._get_user_config(user_id).get("symbol_thresholds", {}))
                        else:
                            user_id = ""
                            st = dict(server_ref.config.get("symbol_thresholds", {}))
                        current = dict(st.get(symbol, {})) if isinstance(st.get(symbol), dict) else {}
                        if has_score:
                            if score is None:
                                current.pop("anomaly_score", None)
                                if server_ref.detector and not server_ref.user_config_store:
                                    server_ref.detector.remove_symbol_threshold(symbol)
                            else:
                                current["anomaly_score"] = float(score)
                                if server_ref.detector and not server_ref.user_config_store:
                                    server_ref.detector.set_symbol_threshold(symbol, float(score))
                        if has_push_rules:
                            if enabled_trigger_count(push_rules) > 0:
                                current["push_rules"] = push_rules
                            else:
                                current.pop("push_rules", None)
                        if current:
                            st[symbol] = current
                        else:
                            st.pop(symbol, None)
                        if server_ref.user_config_store:
                            server_ref.user_config_store.update_symbol_thresholds(user_id, st)
                            server_ref._notify_user_config_change()
                        else:
                            server_ref.config["symbol_thresholds"] = st
                            server_ref._save_config()
                    self._send_json({"ok": True})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_ai_config_post(self) -> None:
                try:
                    payload = self._read_json()
                    with server_ref._config_lock:
                        if server_ref.user_config_store:
                            user_id = self._user_id()
                            ai_cfg = dict(server_ref._get_user_config(user_id).get("ai", {}))
                        else:
                            user_id = ""
                            ai_cfg = dict(server_ref.config.get("ai", {}))
                        for key in (
                            "enabled",
                            "provider",
                            "api_key",
                            "model",
                            "base_url",
                            "activation_threshold",
                            "cache_ttl_seconds",
                            "retry_cooldown_seconds",
                            "request_timeout_seconds",
                            "max_tokens",
                            "triggers",
                        ):
                            if key in payload:
                                ai_cfg[key] = payload[key]
                        if server_ref.user_config_store:
                            server_ref.user_config_store.update_ai(user_id, ai_cfg)
                            server_ref._get_ai_analyzer(user_id, ai_cfg)
                        else:
                            server_ref.config["ai"] = ai_cfg
                            if "api_key" in payload:
                                server_ref.config["_ai_api_key_runtime_only"] = True
                            if server_ref.ai_analyzer:
                                server_ref.ai_analyzer.update_config(ai_cfg)
                            server_ref._save_config()
                    self._send_json({"ok": True})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)

            def _handle_ai_analysis_get(self) -> None:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                symbol = (params.get("symbol", [""])[0]).upper()
                force_refresh = (params.get("force", ["0"])[0] == "1")
                if not symbol:
                    self._send_json({"analysis": None, "reason": "symbol required"})
                    return
                user_id = self._user_id()
                analyzer = server_ref._get_ai_analyzer(user_id)
                if not analyzer or not analyzer.enabled:
                    self._send_json({"analysis": None, "reason": "ai disabled"})
                    return
                snapshot_data = state.get_symbol_data(symbol)
                if not snapshot_data:
                    self._send_json({"analysis": None, "reason": "no data"})
                    return
                cached = analyzer.get_cached(symbol)
                if cached and not force_refresh:
                    self._send_json({"analysis": cached, "cached": True})
                    return
                loop = server_ref._event_loop
                if loop:
                    import asyncio
                    future = asyncio.run_coroutine_threadsafe(
                        analyzer.analyze(symbol, snapshot_data, force=force_refresh), loop
                    )
                    try:
                        result = future.result(timeout=35)
                        self._send_json(
                            {
                                "analysis": result,
                                "cached": False,
                                "reason": None
                                if result
                                else (
                                    analyzer.get_last_error()
                                    if analyzer
                                    else "analysis failed"
                                ),
                            }
                        )
                    except Exception:
                        self._send_json({"analysis": None, "reason": "analysis timeout"})
                else:
                    self._send_json({"analysis": None, "reason": "event loop not ready"})

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                return json.loads(body)

            def _require_user(self) -> dict | None:
                if not server_ref.auth_manager or not server_ref.auth_manager.enabled:
                    return {
                        "user_id": self.headers.get("X-CFM-User", "default_user"),
                        "username": "local",
                        "role": "local",
                    }
                auth_header = self.headers.get("Authorization", "")
                prefix = "Bearer "
                if not auth_header.startswith(prefix):
                    self._send_json({"ok": False, "error": "未登录"}, status=401)
                    return None
                token = auth_header[len(prefix):].strip()
                try:
                    return server_ref.auth_manager.verify_token(token)
                except AuthError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=401)
                    return None

            def _user_id(self) -> str:
                user = getattr(self, "_request_user", None)
                if user and user.get("user_id"):
                    return str(user["user_id"])
                return self.headers.get("X-CFM-User", "default_user")

            def _auth_rate_limited(self, action: str) -> bool:
                window_seconds = 300
                max_attempts = 20 if action == "login" else 8
                now = time()
                client = self.client_address[0] if self.client_address else "unknown"
                key = f"{action}:{client}"
                with server_ref._auth_attempt_lock:
                    attempts = [
                        ts
                        for ts in server_ref._auth_attempts.get(key, [])
                        if now - ts < window_seconds
                    ]
                    limited = len(attempts) >= max_attempts
                    if not limited:
                        attempts.append(now)
                    server_ref._auth_attempts[key] = attempts
                    return limited

            def _send_json(self, data: dict, status: int = 200) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_text(self, text: str, content_type: str) -> None:
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>合约异动监控</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111316;
      --panel: #1a1d22;
      --panel-2: #20252b;
      --text: #e8edf2;
      --muted: #8d99a6;
      --line: #303741;
      --green: #2bd576;
      --red: #ff5a66;
      --amber: #f2b84b;
      --blue: #64a8ff;
      --blue-soft: rgba(100, 168, 255, .14);
      --green-soft: rgba(43, 213, 118, .12);
      --red-soft: rgba(255, 90, 102, .12);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      height: 100vh;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
      overflow: hidden;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: #15181c;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
    }

    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .scope-btn {
      height: 28px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-2);
      color: var(--text);
      padding: 0 10px;
      font-size: 12px;
      cursor: pointer;
      max-width: 210px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .scope-btn:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto auto auto;
      gap: 10px;
      width: min(900px, 100%);
    }

    .symbol-input {
      width: 100%;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101215;
      color: var(--text);
      padding: 0 11px;
      outline: none;
      font-size: 13px;
    }

    .symbol-input:focus {
      border-color: var(--blue);
    }

    .save-btn {
      height: 36px;
      border: 1px solid #3a4655;
      border-radius: 6px;
      background: #243244;
      color: var(--text);
      padding: 0 14px;
      font-size: 13px;
      cursor: pointer;
    }

    .save-btn:hover {
      border-color: var(--blue);
    }

    .save-btn.primary {
      background: #1267d6;
      border-color: #1267d6;
      color: #ffffff;
      font-weight: 650;
    }

    .save-btn.ai {
      background: rgba(43, 213, 118, .12);
      border-color: rgba(43, 213, 118, .32);
      color: var(--green);
    }

    .save-btn.ghost {
      background: transparent;
    }

    .save-btn.danger {
      background: var(--red-soft);
      border-color: rgba(255, 90, 102, .32);
      color: var(--red);
    }

    body.light {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-2: #edf1f5;
      --text: #18202a;
      --muted: #647184;
      --line: #d8e0ea;
      --green: #098b4d;
      --red: #d92d3a;
      --amber: #b7791f;
      --blue: #1267d6;
      --blue-soft: rgba(18, 103, 214, .12);
      --green-soft: rgba(9, 139, 77, .12);
      --red-soft: rgba(217, 45, 58, .1);
    }

    body.light header,
    body.light .modal-card,
    body.light .auth-card {
      background: #ffffff;
    }

    body.light .symbol-input,
    body.light .setting-group input,
    body.light .setting-group select,
    body.light .modal-close,
    body.light .auth-form input {
      background: #ffffff;
      color: var(--text);
    }

    body.light .save-btn,
    body.light .small-btn {
      background: #edf4ff;
      color: var(--text);
      border-color: #bfd4f2;
    }

    body.light .small-btn.secondary {
      background: #eef1f5;
    }

    body.light .scope-btn {
      background: #edf4ff;
    }

    body.light .metric,
    body.light .score,
    body.light .risk,
    body.light .tag,
    body.light .chip {
      background: #f5f7fa;
    }

    body.light .summary-chip,
    body.light .detail-callout,
    body.light .metric,
    body.light .event,
    body.light .detail {
      background: #f8fafc;
      border-color: #dbe4ef;
    }

    body.light .detail-callout.primary,
    body.light .ai-inline {
      background: #eef5ff;
      border-color: #cadef7;
    }

    body.light .chart-card,
    body.light .mini-meter,
    body.light .insight-card,
    body.light .ai-card,
    body.light .range-rail-card,
    body.light .depth-card,
    body.light .detail-tab {
      background: #f8fafc;
      border-color: #dbe4ef;
    }

    body.light .detail-tab.active {
      background: #eaf2ff;
      border-color: #cadef7;
    }

    body.light .section-title {
      background: #fbfcfe;
    }

    body.light .events {
      box-shadow: 0 24px 70px rgba(31, 55, 91, .14);
    }

    body.light th {
      background: #edf1f5;
    }

    body.light tbody tr:hover {
      background: rgba(18, 103, 214, .06);
    }

    body.light tbody tr.selected {
      background: rgba(18, 103, 214, .12);
    }

    body.light .ai-inline {
      background: rgba(18, 103, 214, .06);
      border-color: rgba(18, 103, 214, .18);
    }

    .auth-screen {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(8, 12, 18, .82);
      backdrop-filter: blur(8px);
      z-index: 200;
    }

    .auth-screen.open {
      display: flex;
    }

    .auth-card {
      width: min(420px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #15181c;
      box-shadow: 0 18px 70px rgba(0, 0, 0, .52);
      padding: 22px;
    }

    .auth-title {
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 750;
    }

    .auth-hint {
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .auth-form {
      display: grid;
      gap: 12px;
    }

    .auth-form input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101215;
      color: var(--text);
      padding: 0 11px;
      outline: none;
      font-size: 13px;
    }

    .auth-form input:focus {
      border-color: var(--blue);
    }

    .auth-error {
      min-height: 18px;
      color: var(--red);
      font-size: 12px;
      line-height: 1.4;
    }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(6, 10, 14, .72);
      backdrop-filter: blur(5px);
      z-index: 100;
    }

    .modal-backdrop.open {
      display: flex;
    }

    .modal-card {
      width: min(620px, calc(100vw - 32px));
      max-height: min(78vh, 760px);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #15181c;
      box-shadow: 0 18px 60px rgba(0, 0, 0, .45);
    }

    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
    }

    .modal-title {
      font-size: 15px;
      font-weight: 650;
    }

    .modal-close {
      width: 32px;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101215;
      color: var(--muted);
      cursor: pointer;
      font-size: 16px;
    }

    .modal-close:hover {
      color: var(--text);
      border-color: var(--blue);
    }

    .modal-body {
      display: grid;
      gap: 14px;
      padding: 18px;
    }

    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding-top: 4px;
    }

    .setting-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .setting-group label {
      font-size: 12px;
      color: var(--muted);
    }
    .setting-group input, .setting-group select {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #101215;
      color: var(--text);
      padding: 0 10px;
      font-size: 13px;
      outline: none;
      min-width: 140px;
    }
    .setting-group input:focus, .setting-group select:focus { border-color: var(--blue); }
    .setting-group select { padding-right: 24px; }

    .setting-help {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
    }

    .profile-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .profile-item {
      min-width: 0;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, .02);
    }

    .profile-label {
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 11px;
    }

    .profile-value {
      min-width: 0;
      color: var(--text);
      font-size: 13px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .condition-grid,
    .telegram-users {
      display: grid;
      gap: 8px;
    }

    .condition-row,
    .telegram-user {
      display: grid;
      grid-template-columns: auto minmax(90px, 1fr) minmax(78px, 110px);
      align-items: center;
      gap: 8px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, .02);
      font-size: 12px;
    }

    .condition-row input[type="checkbox"] {
      width: 16px;
      min-width: 16px;
      height: 16px;
    }

    .condition-row input[type="number"] {
      min-width: 0;
      width: 100%;
    }

    .telegram-user {
      grid-template-columns: minmax(72px, .8fr) minmax(120px, 1.1fr) minmax(120px, 1fr) auto;
    }

    .telegram-user input {
      min-width: 0;
      width: 100%;
    }

    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 14px;
      font-size: 12px;
    }
    .chip-x {
      cursor: pointer;
      color: var(--muted);
      font-size: 14px;
      line-height: 1;
    }
    .chip-x:hover { color: var(--red); }
    .setting-row {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      flex-wrap: wrap;
    }
    .small-btn {
      height: 32px;
      border: 1px solid #3a4655;
      border-radius: 5px;
      background: #243244;
      color: var(--text);
      padding: 0 12px;
      font-size: 12px;
      cursor: pointer;
    }
    .small-btn:hover { border-color: var(--blue); }
    .small-btn.secondary {
      background: #1d2128;
    }
    .small-btn.primary {
      background: #1267d6;
      border-color: #1267d6;
      color: #ffffff;
      font-weight: 650;
    }
    .small-btn.danger {
      background: var(--red-soft);
      border-color: rgba(255, 90, 102, .35);
      color: var(--red);
    }
    .toggle-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .toggle {
      width: 36px; height: 20px;
      border-radius: 10px;
      background: var(--line);
      position: relative;
      cursor: pointer;
      transition: background .2s;
    }
    .toggle.active { background: var(--blue); }
    .toggle::after {
      content: "";
      position: absolute;
      top: 3px; left: 3px;
      width: 14px; height: 14px;
      border-radius: 50%;
      background: var(--text);
      transition: left .2s;
    }
    .toggle.active::after { left: 19px; }

    .symbol-meta {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
      min-width: 0;
    }

    .sub-pill {
      display: inline-flex;
      align-items: center;
      min-width: 42px;
      color: var(--muted);
      font-size: 12px;
    }

    .row-action-btn {
      height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      padding: 0 9px;
      font-size: 11px;
      cursor: pointer;
      flex: 0 0 auto;
    }

    .row-action-btn:hover {
      border-color: var(--blue);
      color: var(--blue);
      background: var(--blue-soft);
    }

    .row-action-btn.active {
      border-color: rgba(18, 103, 214, .35);
      color: var(--blue);
      background: var(--blue-soft);
    }

    .detail-title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }

    .detail-tools {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .collapse-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }

    .collapse-bar .collapse-head {
      flex: 1 1 auto;
    }

    .collapse-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      width: 100%;
      min-width: 0;
      border: 0;
      background: none;
      color: var(--text);
      padding: 0;
      cursor: pointer;
      text-align: left;
      font: inherit;
    }

    .collapse-main {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .collapse-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    .collapse-meta {
      color: var(--muted);
      font-size: 11px;
    }

    .collapse-side {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .collapse-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--panel-2);
      color: var(--muted);
      font-size: 14px;
      font-weight: 750;
      line-height: 1;
    }

    .collapse-head:hover .collapse-title,
    .collapse-head:hover .collapse-icon {
      color: var(--blue);
      border-color: var(--blue);
    }

    .collapsible.collapsed .collapsible-body,
    .event-list.collapsed-list {
      display: none;
    }

    .metric-summary {
      display: none;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
      margin-top: 2px;
    }

    .metric-summary.always-visible {
      display: grid;
      margin: 0 0 12px;
    }

    .collapsible.collapsed .metric-summary {
      display: grid;
    }

    .summary-chip {
      min-width: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      height: 34px;
      padding: 0 9px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .02);
      font-size: 12px;
    }

    .summary-chip.empty .summary-value {
      color: var(--muted);
    }

    .summary-label {
      min-width: 0;
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .summary-value {
      flex: 0 0 auto;
      color: var(--text);
      font-weight: 750;
      white-space: nowrap;
    }

    .section-title.collapsible-title {
      padding: 12px 16px;
    }

    .section-collapse {
      min-height: 28px;
    }

    .inline-link {
      flex: 0 0 auto;
      min-width: 46px;
      height: 24px;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--panel-2);
      color: var(--blue);
      cursor: pointer;
      font-size: 12px;
      line-height: 22px;
      padding: 0 10px;
      white-space: nowrap;
    }

    .inline-link:hover {
      border-color: var(--blue);
      background: rgba(100, 168, 255, .12);
    }

    .ai-inline {
      margin-top: 8px;
      padding: 10px 12px;
      background: rgba(100, 168, 255, .06);
      border: 1px solid rgba(100, 168, 255, .15);
      border-radius: 6px;
      max-height: 220px;
      overflow: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
    }

    .ai-status {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 11px;
    }

    .ai-status strong {
      color: var(--blue);
      font-weight: 650;
    }

    .ai-inline-title {
      color: var(--blue);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 6px;
    }

    .ai-inline-content {
      display: grid;
      gap: 4px;
      font-size: 12px;
      line-height: 1.6;
      color: var(--text);
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(43, 213, 118, .12);
    }

    main {
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr);
      gap: 18px;
      flex: 1;
      min-height: 0;
      overflow: hidden;
      padding: 18px;
    }

    section {
      min-width: 0;
      min-height: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .market {
      display: flex;
      flex-direction: column;
    }

    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    th, td {
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    th {
      color: var(--muted);
      font-weight: 550;
      background: var(--panel-2);
    }

    th:first-child, td:first-child { text-align: left; }
    tr:last-child td { border-bottom: 0; }
    tbody tr {
      cursor: pointer;
    }
    tbody tr:hover {
      background: rgba(100, 168, 255, .05);
    }
    tbody tr.selected {
      background: rgba(100, 168, 255, .14);
      box-shadow: inset 3px 0 0 var(--blue);
    }
    tbody tr.selected td {
      background: rgba(100, 168, 255, .03);
    }
    .symbol { font-weight: 700; }
    .cell-sub {
      margin-top: 4px;
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--muted);
      font-size: 12px;
    }
    .muted { color: var(--muted); }
    .up { color: var(--green); }
    .down { color: var(--red); }
    .mixed { color: var(--amber); }
    .score {
      display: inline-flex;
      justify-content: center;
      min-width: 44px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      color: var(--blue);
      font-weight: 700;
    }

    .risk {
      display: inline-flex;
      justify-content: center;
      min-width: 64px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      font-weight: 700;
    }

    .risk-low { color: var(--muted); }
    .risk-mid { color: var(--amber); }
    .risk-high { color: var(--red); }

    .tag {
      display: inline-flex;
      justify-content: center;
      min-width: 42px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      color: var(--text);
      font-weight: 700;
    }

    .bias-up { color: var(--green); }
    .bias-down { color: var(--red); }
    .bias-watch { color: var(--muted); }
    .bias-crowded { color: var(--amber); }

    .detail-drawer-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(6, 10, 14, .54);
      backdrop-filter: blur(2px);
      opacity: 0;
      pointer-events: none;
      transition: opacity .2s ease;
      z-index: 14;
    }

    main.drawer-open .detail-drawer-backdrop {
      opacity: 1;
      pointer-events: auto;
    }

    .events {
      position: absolute;
      top: 0;
      right: 0;
      bottom: 0;
      width: min(860px, calc(100vw - 48px));
      display: flex;
      flex-direction: column;
      height: auto;
      z-index: 16;
      box-shadow: 0 18px 60px rgba(0, 0, 0, .36);
      transform: translateX(calc(100% + 22px));
      opacity: 0;
      pointer-events: none;
      transition: transform .24s ease, opacity .24s ease;
    }

    .events.open {
      transform: translateX(0);
      opacity: 1;
      pointer-events: auto;
    }

    .drawer-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }

    .drawer-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .drawer-close {
      width: 30px;
      height: 30px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--muted);
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
    }

    .drawer-close:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .drawer-empty {
      display: grid;
      gap: 8px;
      padding: 24px 18px;
    }

    .drawer-empty-title {
      font-size: 16px;
      font-weight: 750;
    }

    .drawer-empty-copy {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }

    .side-scroll {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
    }

    .detail {
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }

    .detail-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }

    .detail-symbol {
      font-size: 18px;
      font-weight: 750;
    }

    .detail-meta {
      min-width: 0;
    }

    .detail-price-wrap {
      margin-top: 10px;
    }

    .detail-price-label {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }

    .detail-price {
      font-size: 24px;
      font-weight: 750;
      line-height: 1.1;
    }

    .detail-bias {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .data-banner {
      margin: 0 0 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.55;
    }

    .data-banner.partial {
      color: var(--amber);
      background: rgba(242, 184, 75, .08);
      border-color: rgba(242, 184, 75, .22);
    }

    .data-banner.empty {
      color: var(--muted);
      background: rgba(255, 255, 255, .02);
    }

    .detail-callout-grid {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 8px;
      margin-bottom: 12px;
    }

    .detail-callout {
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .03);
    }

    .detail-callout.primary {
      background: var(--blue-soft);
      border-color: rgba(100, 168, 255, .2);
    }

    .callout-kicker {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 6px;
    }

    .callout-title {
      font-size: 15px;
      font-weight: 750;
      line-height: 1.25;
      margin-bottom: 5px;
    }

    .callout-copy {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }

    .callout-title.up { color: var(--green); }
    .callout-title.down { color: var(--red); }
    .callout-title.mixed { color: var(--amber); }
    .callout-title.muted { color: var(--text); }

    .metric-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin: 12px 0;
    }

    .metric {
      min-width: 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .025);
    }

    .metric-label {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 6px;
    }

    .metric-value {
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .metric-copy {
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
    }

    .metric-grid.compact .metric {
      padding: 9px 10px;
    }

    .metric-grid.compact .metric-value {
      font-size: 12px;
    }

    .detail-block {
      margin-top: 12px;
    }

    .detail-body-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.18fr) minmax(280px, .94fr);
      gap: 12px;
      align-items: start;
    }

    .detail-column {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .detail-column .detail-block {
      margin-top: 0;
    }

    .detail-title {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }

    .detail-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      color: var(--text);
      font-size: 12px;
      line-height: 1.45;
    }

    .visual-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
      margin-bottom: 12px;
    }

    .chart-card {
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .025);
    }

    .chart-card.span-2 {
      grid-column: span 2;
    }

    .chart-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }

    .chart-title {
      font-size: 13px;
      font-weight: 700;
      line-height: 1.25;
    }

    .chart-meta {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
      text-align: right;
      white-space: nowrap;
    }

    .spark-wrap {
      height: 92px;
      color: var(--blue);
    }

    .spark-wrap.short {
      height: 72px;
    }

    .spark-wrap.up {
      color: var(--green);
    }

    .spark-wrap.down {
      color: var(--red);
    }

    .spark-wrap.mixed {
      color: var(--amber);
    }

    .spark-svg {
      width: 100%;
      height: 100%;
      display: block;
    }

    .spark-grid-line {
      stroke: currentColor;
      opacity: .11;
      stroke-width: .9;
    }

    .spark-line {
      fill: none;
      stroke: currentColor;
      stroke-width: 2.2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }

    .spark-area {
      fill: currentColor;
      opacity: .08;
    }

    .spark-dot {
      fill: currentColor;
    }

    .spark-bar {
      fill: currentColor;
      opacity: .78;
    }

    .chart-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100%;
      color: var(--muted);
      font-size: 12px;
      border: 1px dashed var(--line);
      border-radius: 6px;
    }

    .meter-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }

    .mini-meter {
      min-width: 0;
      padding: 10px 11px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .025);
    }

    .meter-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 7px;
      font-size: 12px;
    }

    .meter-head span {
      color: var(--muted);
    }

    .meter-head strong {
      font-size: 13px;
      font-weight: 750;
      white-space: nowrap;
    }

    .meter-track {
      position: relative;
      height: 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .06);
      overflow: hidden;
    }

    .meter-fill {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--blue);
    }

    .meter-fill.up {
      background: var(--green);
    }

    .meter-fill.down {
      background: var(--red);
    }

    .meter-fill.mixed {
      background: var(--amber);
    }

    .meter-copy {
      margin-top: 7px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
    }

    .detail-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }

    .detail-tab {
      height: 30px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255, 255, 255, .025);
      color: var(--muted);
      padding: 0 12px;
      font-size: 12px;
      cursor: pointer;
    }

    .detail-tab:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .detail-tab.active {
      background: var(--blue-soft);
      border-color: rgba(100, 168, 255, .28);
      color: var(--blue);
      font-weight: 700;
    }

    .detail-panel {
      display: grid;
      gap: 12px;
    }

    .insight-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .insight-card,
    .ai-card,
    .range-rail-card,
    .depth-card {
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .025);
    }

    .insight-kicker {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 6px;
    }

    .insight-value {
      font-size: 15px;
      font-weight: 750;
      line-height: 1.25;
      margin-bottom: 5px;
    }

    .insight-copy {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }

    .reason-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .reason-pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .04);
      border: 1px solid var(--line);
      color: var(--text);
      font-size: 12px;
      line-height: 1.35;
    }

    .range-rail-card {
      display: grid;
      gap: 10px;
    }

    .range-rail-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    .range-rail-title {
      font-size: 13px;
      font-weight: 700;
    }

    .range-rail-meta {
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }

    .range-rail {
      position: relative;
      height: 70px;
    }

    .range-rail-track {
      position: absolute;
      left: 0;
      right: 0;
      top: 30px;
      height: 10px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(43, 213, 118, .18), rgba(100, 168, 255, .18), rgba(255, 90, 102, .18));
      border: 1px solid rgba(255, 255, 255, .06);
    }

    .range-rail-marker {
      position: absolute;
      top: 0;
      width: 0;
      height: 64px;
      z-index: 1;
    }

    .range-rail-marker strong {
      position: absolute;
      top: 0;
      left: var(--label-offset, 0px);
      display: flex;
      align-items: center;
      justify-content: center;
      min-width: 46px;
      height: 22px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      font-size: 11px;
      white-space: nowrap;
      line-height: 1;
      transform: translateX(-50%);
    }

    .range-rail-marker strong::after {
      content: "";
      position: absolute;
      left: 50%;
      top: 100%;
      width: 1px;
      height: 5px;
      background: currentColor;
      opacity: .5;
      transform: translateX(-50%);
    }

    .range-rail-marker.compact strong {
      min-width: 42px;
      height: 20px;
      padding: 0 6px;
      font-size: 10px;
    }

    .range-rail-marker .marker-leader {
      position: absolute;
      top: 27px;
      left: 0;
      display: none;
      height: 1px;
      width: var(--leader-width, 0px);
      background: currentColor;
      opacity: .42;
    }

    .range-rail-marker.has-shift .marker-leader {
      display: block;
    }

    .range-rail-marker.shift-left .marker-leader {
      left: calc(var(--leader-width, 0px) * -1);
    }

    .range-rail-marker.shift-right .marker-leader {
      left: 0;
    }

    .range-rail-marker .marker-stem {
      position: absolute;
      top: 27px;
      left: 0;
      width: 2px;
      height: 32px;
      border-radius: 999px;
      background: currentColor;
    }

    .range-rail-marker.lane-1 {
      top: 16px;
      z-index: 0;
    }

    .range-rail-footer {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }

    .rail-stat {
      min-width: 0;
    }

    .rail-stat-label {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }

    .rail-stat-value {
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .ai-panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .ai-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }

    .ai-card-title {
      color: var(--blue);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
    }

    .ai-card-copy {
      color: var(--text);
      font-size: 12px;
      line-height: 1.6;
      overflow-wrap: anywhere;
    }

    .depth-split {
      display: grid;
      gap: 10px;
    }

    .depth-track {
      display: flex;
      width: 100%;
      height: 12px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255, 255, 255, .05);
      border: 1px solid rgba(255, 255, 255, .04);
    }

    .depth-track span:first-child {
      background: rgba(43, 213, 118, .7);
    }

    .depth-track span:last-child {
      background: rgba(255, 90, 102, .7);
    }

    .depth-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
    }

    .detail-placeholder {
      padding: 16px 14px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
      text-align: center;
    }

    .event-list {
      min-height: 0;
    }

    .table-wrap {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
    }

    .event {
      display: grid;
      gap: 9px;
      padding: 13px 14px;
      border-bottom: 1px solid var(--line);
    }

    .event-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }

    .event-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .event-main {
      min-width: 0;
    }

    .event-score-text {
      color: var(--blue);
      font-weight: 750;
    }

    .event-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      min-width: 74px;
      height: 34px;
      padding: 0 10px;
      border-radius: 6px;
      background: var(--blue-soft);
      color: var(--blue);
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }

    .event-badge.up {
      background: var(--green-soft);
      color: var(--green);
    }

    .event-badge.down {
      background: var(--red-soft);
      color: var(--red);
    }

    .event-badge.mixed {
      background: rgba(242, 184, 75, .12);
      color: var(--amber);
    }

    .event-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 8px;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .event-row {
      display: grid;
      grid-template-columns: 38px minmax(0, 1fr);
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }

    .event-label {
      color: var(--muted);
      font-weight: 650;
    }

    .event-text {
      min-width: 0;
      color: var(--text);
      overflow-wrap: anywhere;
    }

    .event-text.muted {
      color: var(--muted);
    }

    .reason {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }

    .empty {
      padding: 22px 16px;
      color: var(--muted);
      font-size: 13px;
    }

    @media (max-width: 980px) {
      body { overflow: auto; }
      main {
        grid-template-columns: 1fr;
        overflow: visible;
      }
      .detail-callout-grid {
        grid-template-columns: 1fr;
      }
      .metric-summary,
      .metric-summary.always-visible,
      .collapsible.collapsed .metric-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .visual-grid,
      .insight-grid,
      .ai-grid,
      .range-rail-footer {
        grid-template-columns: 1fr;
      }
      .chart-card.span-2 {
        grid-column: span 1;
      }
      .meter-grid {
        grid-template-columns: 1fr;
      }
      .detail-body-grid {
        grid-template-columns: 1fr;
      }
      .events {
        width: min(860px, calc(100vw - 20px));
      }
    }

    @media (max-width: 720px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      main { padding: 10px; }
      section { border-radius: 6px; }
      table { min-width: 1080px; }
      .profile-grid { grid-template-columns: 1fr; }
      .events {
        width: calc(100vw - 20px);
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>合约异动监控</h1>
    <div class="toolbar">
      <input id="symbol-input" class="symbol-input" autocomplete="off" spellcheck="false" placeholder="BTCUSDT, ETHUSDT, SOLUSDT">
      <button id="save-symbols" class="save-btn primary">保存监控</button>
      <button id="btn-telegram" class="save-btn" title="推送设置">推送</button>
      <button id="btn-ai" class="save-btn ai" title="AI 设置">AI</button>
      <button id="btn-theme" class="save-btn ghost" title="切换主题">白天</button>
      <button id="btn-logout" class="save-btn danger" title="退出登录">退出</button>
    </div>
    <div class="status"><span class="dot"></span><span id="updated">等待数据</span><button id="user-scope" class="scope-btn" type="button">个人配置</button></div>
  </header>

  <div class="auth-screen" id="auth-screen">
    <div class="auth-card">
      <h2 class="auth-title" id="auth-title">登录</h2>
      <div class="auth-hint" id="auth-hint">登录后加载你的监控列表、AI、Telegram 和阈值配置。</div>
      <div class="auth-form">
        <input id="auth-username" autocomplete="username" placeholder="用户名">
        <input id="auth-password" autocomplete="current-password" type="password" placeholder="密码">
        <div class="auth-error" id="auth-error"></div>
        <button class="small-btn primary" id="auth-submit" type="button">登录</button>
        <button class="small-btn secondary" id="auth-switch" type="button">创建账号</button>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="telegram-modal">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title">Telegram 推送设置</div>
        <button id="close-telegram-modal" class="modal-close" type="button">x</button>
      </div>
      <div class="modal-body">
        <div class="setting-group">
          <label>启用推送</label>
          <div class="toggle-wrap">
            <div class="toggle" id="tg-toggle"></div>
            <span class="muted" id="tg-toggle-label">关闭</span>
          </div>
        </div>
        <div class="setting-group">
          <label>用户推送通道</label>
          <div class="telegram-users" id="tg-users"></div>
          <button class="small-btn secondary" id="tg-add-user-btn" type="button">添加通道</button>
          <div class="setting-help">每个用户可以使用自己的 Bot Token 和 Chat ID；Chat ID 多个时用逗号分隔。</div>
        </div>
        <div class="modal-actions">
          <button class="small-btn secondary" id="close-telegram-btn" type="button">关闭</button>
          <button class="small-btn secondary" id="tg-test-btn" type="button">发送测试</button>
          <button class="small-btn primary" id="tg-save-btn" type="button">保存设置</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="ai-modal">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title">AI 分析设置</div>
        <button id="close-ai-modal" class="modal-close" type="button">x</button>
      </div>
      <div class="modal-body">
        <div class="setting-group">
          <label>启用 AI</label>
          <div class="toggle-wrap">
            <div class="toggle" id="ai-toggle"></div>
            <span class="muted" id="ai-toggle-label">关闭</span>
          </div>
        </div>
        <div class="setting-group">
          <label>Provider</label>
          <select id="ai-provider">
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="openrouter">OpenRouter</option>
            <option value="deepseek">DeepSeek</option>
            <option value="siliconflow">SiliconFlow</option>
            <option value="moonshot">Moonshot</option>
            <option value="dashscope">DashScope</option>
            <option value="custom">自定义 OpenAI 兼容</option>
          </select>
        </div>
        <div class="setting-group">
          <label>Base URL</label>
          <input id="ai-base-url" placeholder="留空则使用该 Provider 默认地址">
          <div class="setting-help">除 Anthropic 外，其它 Provider 按 OpenAI 兼容接口调用；你也可以手动改成代理地址。</div>
        </div>
        <div class="setting-group">
          <label>API Key</label>
          <input id="ai-key" type="password" placeholder="输入 API Key">
          <div class="setting-help">页面输入的 Key 仅用于当前运行进程；长期部署建议写入环境变量。</div>
        </div>
        <div class="setting-group">
          <label>模型</label>
          <input id="ai-model" placeholder="gpt-4o-mini">
        </div>
        <div class="setting-group">
          <label>触发阈值</label>
          <input id="ai-threshold" type="number" value="60" min="0" max="100" style="width:90px">
        </div>
        <div class="setting-group">
          <label>触发条件</label>
          <select id="ai-trigger-mode" style="width:140px">
            <option value="any">任一条件满足</option>
            <option value="all">全部条件满足</option>
          </select>
          <div class="condition-grid">
            <label class="condition-row"><input id="ai-trigger-score" type="checkbox"><span>异常分 >=</span><input id="ai-trigger-score-value" type="number" min="0" max="100" value="60"></label>
            <label class="condition-row"><input id="ai-trigger-volume" type="checkbox"><span>1分钟成交额 >=</span><input id="ai-trigger-volume-value" type="number" min="0" value="500000"></label>
            <label class="condition-row"><input id="ai-trigger-multiplier" type="checkbox"><span>量能倍数 >=</span><input id="ai-trigger-multiplier-value" type="number" min="0" step="0.1" value="3"></label>
            <label class="condition-row"><input id="ai-trigger-price" type="checkbox"><span>1分钟波动绝对值 >=</span><input id="ai-trigger-price-value" type="number" min="0" step="0.1" value="0.8"></label>
            <label class="condition-row"><input id="ai-trigger-oi" type="checkbox"><span>OI 5分钟绝对值 >=</span><input id="ai-trigger-oi-value" type="number" min="0" step="0.1" value="1.5"></label>
            <label class="condition-row"><input id="ai-trigger-liquidation" type="checkbox"><span>1分钟爆仓额 >=</span><input id="ai-trigger-liquidation-value" type="number" min="0" value="250000"></label>
          </div>
        </div>
        <div class="setting-group">
          <label>失败冷却秒数</label>
          <input id="ai-retry-cooldown" type="number" value="120" min="10" max="3600" style="width:110px">
        </div>
        <div class="setting-group">
          <label>请求超时秒数</label>
          <input id="ai-timeout" type="number" value="30" min="5" max="30" style="width:110px">
        </div>
        <div class="modal-actions">
          <button class="small-btn secondary" id="close-ai-btn" type="button">关闭</button>
          <button class="small-btn primary" id="ai-save-btn" type="button">保存设置</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="threshold-modal">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title">推送规则设置</div>
        <button id="close-threshold-modal" class="modal-close" type="button">x</button>
      </div>
      <div class="modal-body">
        <div class="setting-group">
          <label>监测对象</label>
          <input id="threshold-symbol" disabled>
        </div>
        <div class="setting-group">
          <label>异常分阈值</label>
          <input id="threshold-input" type="number" min="0" max="100" placeholder="70">
          <div class="setting-help" id="threshold-hint">留空时回退到全局默认；如果同时配置附加条件，则按组合规则推送。</div>
        </div>
        <div class="setting-group">
          <label>组合模式</label>
          <select id="threshold-trigger-mode" style="width:140px">
            <option value="any">任一条件满足</option>
            <option value="all">全部条件满足</option>
          </select>
        </div>
        <div class="setting-group">
          <label>附加条件</label>
          <div class="condition-grid">
            <label class="condition-row"><input id="threshold-trigger-score" type="checkbox"><span>异常分 >=</span><input id="threshold-trigger-score-value" type="number" min="0" max="100" value="70"></label>
            <label class="condition-row"><input id="threshold-trigger-volume" type="checkbox"><span>1分钟成交额 >=</span><input id="threshold-trigger-volume-value" type="number" min="0" value="500000"></label>
            <label class="condition-row"><input id="threshold-trigger-multiplier" type="checkbox"><span>量能倍数 >=</span><input id="threshold-trigger-multiplier-value" type="number" min="0" step="0.1" value="3"></label>
            <label class="condition-row"><input id="threshold-trigger-price" type="checkbox"><span>1分钟波动绝对值 >=</span><input id="threshold-trigger-price-value" type="number" min="0" step="0.1" value="0.8"></label>
            <label class="condition-row"><input id="threshold-trigger-oi" type="checkbox"><span>OI 5分钟绝对值 >=</span><input id="threshold-trigger-oi-value" type="number" min="0" step="0.1" value="1.5"></label>
            <label class="condition-row"><input id="threshold-trigger-liquidation" type="checkbox"><span>1分钟爆仓额 >=</span><input id="threshold-trigger-liquidation-value" type="number" min="0" value="250000"></label>
            <label class="condition-row"><input id="threshold-trigger-imbalance" type="checkbox"><span>盘口失衡绝对值 >=</span><input id="threshold-trigger-imbalance-value" type="number" min="0" step="0.1" value="18"></label>
            <label class="condition-row"><input id="threshold-trigger-depth-drop" type="checkbox"><span>盘口深度下降 >=</span><input id="threshold-trigger-depth-drop-value" type="number" min="0" step="0.1" value="18"></label>
            <label class="condition-row"><input id="threshold-trigger-spread" type="checkbox"><span>盘口点差 >=</span><input id="threshold-trigger-spread-value" type="number" min="0" step="0.1" value="4"></label>
          </div>
          <div class="setting-help">适合做急拉前兆、插针风险、爆仓连锁这类更灵活的推送过滤。</div>
        </div>
        <div class="modal-actions">
          <button class="small-btn danger" id="threshold-reset-btn" type="button">恢复默认</button>
          <button class="small-btn secondary" id="close-threshold-btn" type="button">关闭</button>
          <button class="small-btn primary" id="threshold-save-btn" type="button">保存</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="profile-modal">
    <div class="modal-card">
      <div class="modal-head">
        <div class="modal-title">个人配置</div>
        <button id="close-profile-modal" class="modal-close" type="button">x</button>
      </div>
      <div class="modal-body">
        <div class="profile-grid">
          <div class="profile-item">
            <div class="profile-label">账号</div>
            <div class="profile-value" id="profile-name">--</div>
          </div>
          <div class="profile-item">
            <div class="profile-label">角色</div>
            <div class="profile-value" id="profile-role">--</div>
          </div>
          <div class="profile-item">
            <div class="profile-label">用户 ID</div>
            <div class="profile-value" id="profile-user-id">--</div>
          </div>
          <div class="profile-item">
            <div class="profile-label">认证</div>
            <div class="profile-value" id="profile-auth">--</div>
          </div>
          <div class="profile-item">
            <div class="profile-label">监控数量</div>
            <div class="profile-value" id="profile-symbol-count">0 个合约</div>
          </div>
          <div class="profile-item">
            <div class="profile-label">主题</div>
            <div class="profile-value" id="profile-theme">夜间</div>
          </div>
        </div>
        <div class="setting-group">
          <label>当前监控</label>
          <div class="chip-list" id="profile-symbols"></div>
        </div>
        <div class="modal-actions">
          <button class="small-btn secondary" id="profile-open-telegram" type="button">推送设置</button>
          <button class="small-btn secondary" id="profile-open-ai" type="button">AI 设置</button>
          <button class="small-btn danger" id="profile-logout" type="button">退出登录</button>
          <button class="small-btn primary" id="close-profile-btn" type="button">关闭</button>
        </div>
      </div>
    </div>
  </div>

  <main id="dashboard-main">
    <div class="detail-drawer-backdrop" id="detail-drawer-backdrop"></div>
    <section class="market">
      <div class="section-title">
        <span>我的 USDT 永续合约</span>
        <span id="count">0 个合约</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>异常分</th>
              <th>风险</th>
              <th>倾向</th>
              <th>价格</th>
              <th>1分钟</th>
              <th>5分钟</th>
              <th>1分钟成交额</th>
              <th>放大倍数</th>
              <th>OI 5分钟</th>
              <th>爆仓1m</th>
              <th>点差</th>
            </tr>
          </thead>
          <tbody id="symbols"></tbody>
        </table>
      </div>
    </section>

    <section class="events" id="detail-drawer">
      <div class="section-title">
        <div class="drawer-head">
          <span>合约详情</span>
          <div class="drawer-meta">
            <span id="source-label">REST</span>
            <button class="drawer-close" id="detail-close-btn" type="button">x</button>
          </div>
        </div>
      </div>
      <div class="side-scroll" id="side-scroll">
        <div class="detail" id="detail"></div>
        <div class="section-title collapsible-title">
          <button class="collapse-head section-collapse" id="events-collapse-btn" data-collapse="events" type="button" aria-expanded="true">
            <span class="collapse-main">
              <span class="collapse-title">最近报警</span>
            </span>
            <span class="collapse-side">
              <span id="alert-count">0</span>
              <span class="collapse-icon">-</span>
            </span>
          </button>
        </div>
        <div class="event-list" id="events"></div>
      </div>
    </section>
  </main>

  <script>
    const mainEl = document.getElementById("dashboard-main");
    const detailDrawerBackdropEl = document.getElementById("detail-drawer-backdrop");
    const detailDrawerEl = document.getElementById("detail-drawer");
    const detailCloseBtn = document.getElementById("detail-close-btn");
    const symbolsEl = document.getElementById("symbols");
    const eventsEl = document.getElementById("events");
    const eventsCollapseBtn = document.getElementById("events-collapse-btn");
    const sideScrollEl = document.getElementById("side-scroll");
    const updatedEl = document.getElementById("updated");
    const countEl = document.getElementById("count");
    const alertCountEl = document.getElementById("alert-count");
    const symbolInputEl = document.getElementById("symbol-input");
    const saveSymbolsEl = document.getElementById("save-symbols");
    const btnTheme = document.getElementById("btn-theme");
    const btnLogout = document.getElementById("btn-logout");
    const userScopeEl = document.getElementById("user-scope");
    const detailEl = document.getElementById("detail");
    const sourceLabelEl = document.getElementById("source-label");
    const telegramModal = document.getElementById("telegram-modal");
    const aiModal = document.getElementById("ai-modal");
    const thresholdModal = document.getElementById("threshold-modal");
    const profileModal = document.getElementById("profile-modal");
    const thresholdSymbolEl = document.getElementById("threshold-symbol");
    const thresholdInputEl = document.getElementById("threshold-input");
    const thresholdHintEl = document.getElementById("threshold-hint");
    const thresholdTriggerMode = document.getElementById("threshold-trigger-mode");
    const profileNameEl = document.getElementById("profile-name");
    const profileRoleEl = document.getElementById("profile-role");
    const profileUserIdEl = document.getElementById("profile-user-id");
    const profileAuthEl = document.getElementById("profile-auth");
    const profileSymbolCountEl = document.getElementById("profile-symbol-count");
    const profileThemeEl = document.getElementById("profile-theme");
    const profileSymbolsEl = document.getElementById("profile-symbols");
    const authScreen = document.getElementById("auth-screen");
    const authTitle = document.getElementById("auth-title");
    const authHint = document.getElementById("auth-hint");
    const authUsername = document.getElementById("auth-username");
    const authPassword = document.getElementById("auth-password");
    const authError = document.getElementById("auth-error");
    const authSubmit = document.getElementById("auth-submit");
    const authSwitch = document.getElementById("auth-switch");

    let selectedSymbol = null;
    let inputTouched = false;
    let symbolThresholds = {};
    let globalThreshold = 70;
    let thresholdEditingSymbol = null;
    let aiResults = {};
    let aiRequestedAt = {};
    let aiMeta = {};
    let authStatus = { enabled: false, has_users: false, allow_registration: true };
    let authMode = "login";
    let currentUser = null;
    let authToken = localStorage.getItem("cfm_auth_token") || "";
    let refreshTimer = null;
    let lastSymbols = [];
    let drawerScrollBySymbol = {};
    let aiScrollBySymbol = {};
    let detailInteractionUntil = 0;
    let detailInteractionTimer = null;
    let pendingDetailRefresh = false;
    let pendingAIRefreshSymbol = null;
    const DETAIL_INTERACTION_LOCK_MS = 900;
    const thresholdTriggerFields = {
      score: [document.getElementById("threshold-trigger-score"), document.getElementById("threshold-trigger-score-value")],
      quote_volume_1m: [document.getElementById("threshold-trigger-volume"), document.getElementById("threshold-trigger-volume-value")],
      volume_multiplier: [document.getElementById("threshold-trigger-multiplier"), document.getElementById("threshold-trigger-multiplier-value")],
      price_move_pct_1m_abs: [document.getElementById("threshold-trigger-price"), document.getElementById("threshold-trigger-price-value")],
      oi_change_pct_5m_abs: [document.getElementById("threshold-trigger-oi"), document.getElementById("threshold-trigger-oi-value")],
      liquidation_total_quote_1m: [document.getElementById("threshold-trigger-liquidation"), document.getElementById("threshold-trigger-liquidation-value")],
      depth_imbalance_abs: [document.getElementById("threshold-trigger-imbalance"), document.getElementById("threshold-trigger-imbalance-value")],
      depth_drop_pct_1m: [document.getElementById("threshold-trigger-depth-drop"), document.getElementById("threshold-trigger-depth-drop-value")],
      spread_bps: [document.getElementById("threshold-trigger-spread"), document.getElementById("threshold-trigger-spread-value")]
    };

    const directionText = {
      up: "向上异动",
      down: "向下异动",
      mixed: "混合异常",
      waiting: "等待数据"
    };

    function createUserId() {
      if (window.crypto && window.crypto.randomUUID) {
        return `u_${window.crypto.randomUUID().replace(/-/g, "")}`;
      }
      return `u_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`;
    }

    function getUserId() {
      const key = "cfm_user_id";
      let value = localStorage.getItem(key);
      if (!value) {
        value = createUserId();
        localStorage.setItem(key, value);
      }
      return value;
    }

    const userId = getUserId();
    userScopeEl.textContent = `个人配置 ${userId.slice(-6)}`;

    function storageKey(name) {
      const scope = currentUser && currentUser.user_id ? currentUser.user_id : userId;
      return `${name}_${scope}`;
    }

    function requestHeaders(extra = {}) {
      const headers = Object.assign({ "X-CFM-User": userId }, extra);
      if (authToken) headers.Authorization = `Bearer ${authToken}`;
      return headers;
    }

    async function apiFetch(url, options = {}) {
      const response = await fetch(url, Object.assign({}, options, {
        headers: requestHeaders(options.headers || {})
      }));
      if (response.status === 401 && authStatus.enabled) {
        localStorage.removeItem("cfm_auth_token");
        authToken = "";
        currentUser = null;
        stopRefreshTimer();
        resetViewState();
        showAuth("login");
      }
      return response;
    }

    function stopRefreshTimer() {
      if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
    }

    function isDrawerOpen() {
      return detailDrawerEl.classList.contains("open");
    }

    function setDrawerOpen(open) {
      mainEl.classList.toggle("drawer-open", open);
      detailDrawerEl.classList.toggle("open", open);
    }

    function openDetailDrawer(symbol = null) {
      if (symbol) selectedSymbol = symbol;
      if (!selectedSymbol) return;
      setDrawerOpen(true);
    }

    function closeDetailDrawer(clearSelection = true) {
      setDrawerOpen(false);
      if (clearSelection) {
        clearDeferredDetailState();
        selectedSymbol = null;
        renderSymbols(lastSymbols);
        renderDetail(lastSymbols);
      }
    }

    function resetViewState() {
      selectedSymbol = null;
      inputTouched = false;
      symbolThresholds = {};
      globalThreshold = 70;
      thresholdEditingSymbol = null;
      lastSymbols = [];
      aiResults = {};
      aiRequestedAt = {};
      aiMeta = {};
      drawerScrollBySymbol = {};
      aiScrollBySymbol = {};
      clearDeferredDetailState();
      symbolInputEl.value = "";
      symbolsEl.innerHTML = "";
      eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
      delete detailEl.dataset.symbol;
      detailEl.innerHTML = `
        <div class="drawer-empty">
          <div class="drawer-empty-title">点击左侧合约展开详情</div>
          <div class="drawer-empty-copy">右侧会先给你价格、量能、OI 图，再切到 AI、结构位和流动性视图。</div>
        </div>
      `;
      countEl.textContent = "0 个合约";
      alertCountEl.textContent = "0";
      setDrawerOpen(false);
    }

    function applyTheme(theme) {
      const light = theme === "light";
      document.body.classList.toggle("light", light);
      btnTheme.textContent = light ? "夜间" : "白天";
      localStorage.setItem(storageKey("cfm_theme"), light ? "light" : "dark");
    }

    applyTheme(localStorage.getItem(storageKey("cfm_theme")) || "dark");
    btnTheme.addEventListener("click", () => {
      applyTheme(document.body.classList.contains("light") ? "dark" : "light");
    });

    function updateAuthUser(user) {
      currentUser = user;
      if (user && user.username) {
        userScopeEl.textContent = `${user.username} · 个人配置`;
        userScopeEl.title = "打开个人配置";
      } else {
        userScopeEl.textContent = `个人配置 ${userId.slice(-6)}`;
        userScopeEl.title = "打开个人配置";
      }
    }

    function renderProfileModal() {
      const theme = document.body.classList.contains("light") ? "白天" : "夜间";
      profileNameEl.textContent = currentUser && currentUser.username ? currentUser.username : "本地用户";
      profileRoleEl.textContent = currentUser && currentUser.role ? currentUser.role : (authStatus.enabled ? "未登录" : "local");
      profileUserIdEl.textContent = currentUser && currentUser.user_id ? currentUser.user_id : userId;
      profileAuthEl.textContent = authStatus.enabled ? "JWT 已启用" : "本地模式";
      profileSymbolCountEl.textContent = `${lastSymbols.length} 个合约`;
      profileThemeEl.textContent = theme;
      profileSymbolsEl.innerHTML = lastSymbols.length
        ? lastSymbols.map((symbol) => `<span class="chip">${esc(symbol.symbol || symbol)}</span>`).join("")
        : `<span class="muted">暂无监控对象</span>`;
    }

    function openProfileModal() {
      renderProfileModal();
      openModal(profileModal);
    }

    function showAuth(mode = "login") {
      if (!authStatus.enabled) return;
      authMode = mode;
      const registerMode = authMode === "register";
      authTitle.textContent = registerMode ? "创建管理员账号" : "登录";
      authHint.textContent = registerMode
        ? "首次部署请创建管理员账号。后续用户注册默认关闭，可在配置中开启。"
        : "登录后加载你的监控列表、AI、Telegram 和阈值配置。";
      authSubmit.textContent = registerMode ? "创建并登录" : "登录";
      authSwitch.textContent = registerMode ? "返回登录" : "创建账号";
      authSwitch.style.display = authStatus.allow_registration ? "block" : "none";
      authError.textContent = "";
      authPassword.value = "";
      authScreen.classList.add("open");
      setTimeout(() => authUsername.focus(), 0);
    }

    function hideAuth() {
      authScreen.classList.remove("open");
      authError.textContent = "";
    }

    async function loadAuthStatus() {
      const response = await fetch("/api/auth/status", { cache: "no-store" });
      authStatus = await response.json();
      if (!authStatus.has_users && authStatus.enabled) {
        authStatus.allow_registration = true;
      }
    }

    async function verifyStoredToken() {
      if (!authToken) return false;
      const response = await apiFetch("/api/auth/me", { cache: "no-store" });
      if (!response.ok) return false;
      const data = await response.json();
      if (!data.ok || !data.user) return false;
      updateAuthUser(data.user);
      return true;
    }

    async function submitAuth() {
      const username = authUsername.value.trim();
      const password = authPassword.value;
      authSubmit.disabled = true;
      authSubmit.textContent = authMode === "register" ? "创建中" : "登录中";
      authError.textContent = "";
      try {
        const endpoint = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "认证失败");
        authToken = data.token;
        localStorage.setItem("cfm_auth_token", authToken);
        updateAuthUser(data.user);
        hideAuth();
        startApp();
      } catch (error) {
        authError.textContent = error.message || "认证失败";
      } finally {
        authSubmit.disabled = false;
        authSubmit.textContent = authMode === "register" ? "创建并登录" : "登录";
      }
    }

    function logout() {
      localStorage.removeItem("cfm_auth_token");
      authToken = "";
      updateAuthUser(null);
      stopRefreshTimer();
      resetViewState();
      showAuth("login");
    }

    function startApp() {
      stopRefreshTimer();
      resetViewState();
      authScreen.classList.remove("open");
      applyTheme(localStorage.getItem(storageKey("cfm_theme")) || "dark");
      loadStoredAIResults();
      loadSymbolThresholds().then(refresh);
      refreshTimer = setInterval(refresh, 1000);
    }

    async function bootstrap() {
      try {
        await loadAuthStatus();
        if (!authStatus.enabled) {
          btnLogout.style.display = "none";
          startApp();
          return;
        }
        btnLogout.style.display = "inline-block";
        const ok = await verifyStoredToken();
        if (ok) {
          startApp();
          return;
        }
        showAuth(authStatus.has_users ? "login" : "register");
      } catch (error) {
        updatedEl.textContent = "认证服务不可用";
        showAuth("login");
      }
    }

    authSubmit.addEventListener("click", submitAuth);
    authSwitch.addEventListener("click", () => {
      showAuth(authMode === "register" ? "login" : "register");
    });
    [authUsername, authPassword].forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") submitAuth();
      });
    });
    btnLogout.addEventListener("click", logout);
    userScopeEl.addEventListener("click", openProfileModal);

    function loadStoredAIResults() {
      try {
        const raw = JSON.parse(localStorage.getItem(storageKey("cfm_ai_results")) || "{}");
        const now = Date.now();
        Object.entries(raw).forEach(([symbol, value]) => {
          if (value && value.text && now - Number(value.ts || 0) < 30 * 60 * 1000) {
            aiResults[symbol] = value.text;
          }
        });
      } catch (error) {}
    }

    function saveAIResult(symbol, text) {
      aiResults[symbol] = text;
      try {
        const raw = JSON.parse(localStorage.getItem(storageKey("cfm_ai_results")) || "{}");
        raw[symbol] = { text, ts: Date.now() };
        localStorage.setItem(storageKey("cfm_ai_results"), JSON.stringify(raw));
      } catch (error) {}
    }

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      return Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function mutedValue(text = "--") {
      return `<span class="muted">${esc(text)}</span>`;
    }

    function hasCoreTradeData(symbol) {
      return Boolean(
        symbol &&
        Number(symbol.price || 0) > 0 &&
        Number(symbol.trade_count_1m || 0) > 0 &&
        Number(symbol.updated_at || 0) > 0
      );
    }

    function hasDepthData(symbol) {
      if (!symbol) return false;
      if ((symbol.microstructure_status || "unavailable") !== "active") return false;
      const bidDepth = Number(symbol.bid_depth_notional || 0);
      const askDepth = Number(symbol.ask_depth_notional || 0);
      const spread = Number(symbol.spread_bps || 0);
      return bidDepth > 0 || askDepth > 0 || spread > 0;
    }

    function hasStructureData(symbol) {
      return hasCoreTradeData(symbol) && Number(symbol.support_price || 0) > 0 && Number(symbol.resistance_price || 0) > 0;
    }

    function hasOiData(symbol) {
      if (!symbol) return false;
      return hasCoreTradeData(symbol) && (
        Number(symbol.open_interest || 0) > 0 ||
        Math.abs(Number(symbol.oi_change_pct_5m || 0)) > 0
      );
    }

    function hasFundingData(symbol) {
      if (!symbol) return false;
      return hasCoreTradeData(symbol) && (
        Math.abs(Number(symbol.funding_rate || 0)) > 0 ||
        Number(symbol.open_interest || 0) > 0
      );
    }

    function dataState(symbol) {
      if (hasCoreTradeData(symbol)) return "live";
      if (hasDepthData(symbol)) return "partial";
      return "empty";
    }

    function dataBannerHtml(symbol) {
      const state = dataState(symbol);
      if (state === "live") return "";
      if (state === "partial") {
        return `<div class="data-banner partial">当前已接入盘口深度，但成交主数据暂未到位。价格、波动、量能和结构判断会在实时成交恢复后补齐。</div>`;
      }
      return `<div class="data-banner empty">当前未收到可用成交数据。请优先使用 WebSocket 行情源，避免 REST 受限时页面被默认值填满。</div>`;
    }

    function fmtPctMaybe(value, available = true, digits = 3) {
      if (!available) return mutedValue();
      const number = Number(value || 0);
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(digits)}%</span>`;
    }

    function fmtFundingMaybe(value, available = true) {
      if (!available) return mutedValue();
      const number = Number(value || 0) * 100;
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(4)}%</span>`;
    }

    function fmtBpsMaybe(value, available = true) {
      if (!available) return mutedValue();
      const number = Number(value || 0);
      const cls = number >= 4 ? "down" : number >= 2 ? "mixed" : "muted";
      return `<span class="${cls}">${number.toFixed(2)}</span>`;
    }

    function fmtNumberMaybe(value, digits = 2, available = true, suffix = "") {
      if (!available) return mutedValue();
      return `${fmtNumber(value, digits)}${suffix}`;
    }

    function fmtPlainPctMaybe(value, available = true, digits = 2) {
      if (!available) return "--";
      return fmtPlainPct(value, digits);
    }

    function fmtPriceMaybe(value, available = true) {
      if (!available) return "--";
      return fmtNumber(value, 8);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function seriesValues(values) {
      if (!Array.isArray(values)) return [];
      return values
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value));
    }

    function chartEmptyHtml(text = "暂无可绘制数据") {
      return `<div class="chart-empty">${esc(text)}</div>`;
    }

    function lineChartSvg(values, tone = "mixed") {
      const series = seriesValues(values);
      if (series.length < 2) return chartEmptyHtml("时序样本不足");
      const width = 100;
      const height = 44;
      const top = 4;
      const bottom = 40;
      const min = Math.min(...series);
      const max = Math.max(...series);
      const range = Math.max(max - min, 1e-9);
      const step = width / Math.max(series.length - 1, 1);
      const points = series.map((value, index) => {
        const x = index * step;
        const y = bottom - ((value - min) / range) * (bottom - top);
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      });
      const area = [`0,${bottom}`, ...points, `${width},${bottom}`].join(" ");
      const lastPoint = points[points.length - 1].split(",");
      return `
        <div class="spark-wrap ${tone}">
          <svg class="spark-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            <line class="spark-grid-line" x1="0" y1="12" x2="${width}" y2="12"></line>
            <line class="spark-grid-line" x1="0" y1="26" x2="${width}" y2="26"></line>
            <line class="spark-grid-line" x1="0" y1="40" x2="${width}" y2="40"></line>
            <polygon class="spark-area" points="${area}"></polygon>
            <polyline class="spark-line" points="${points.join(" ")}"></polyline>
            <circle class="spark-dot" cx="${lastPoint[0]}" cy="${lastPoint[1]}" r="2.8"></circle>
          </svg>
        </div>
      `;
    }

    function barChartSvg(values, tone = "blue") {
      const series = seriesValues(values);
      if (!series.length || series.every((value) => value <= 0)) return chartEmptyHtml("暂无量能柱");
      const width = 100;
      const height = 44;
      const max = Math.max(...series, 1);
      const gap = 1.2;
      const barWidth = Math.max((width - gap * (series.length - 1)) / Math.max(series.length, 1), 1.8);
      return `
        <div class="spark-wrap short ${tone}">
          <svg class="spark-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            <line class="spark-grid-line" x1="0" y1="40" x2="${width}" y2="40"></line>
            ${series.map((value, index) => {
              const heightValue = Math.max((value / max) * 34, 2);
              const x = index * (barWidth + gap);
              const y = 40 - heightValue;
              return `<rect class="spark-bar" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${heightValue.toFixed(2)}" rx="1.8"></rect>`;
            }).join("")}
          </svg>
        </div>
      `;
    }

    function meterHtml(label, percent, display, tone = "blue", note = "") {
      const width = clamp(Number(percent) || 0, 0, 100);
      return `
        <div class="mini-meter">
          <div class="meter-head">
            <span>${esc(label)}</span>
            <strong>${display}</strong>
          </div>
          <div class="meter-track">
            <span class="meter-fill ${esc(tone)}" style="width:${width.toFixed(1)}%"></span>
          </div>
          ${note ? `<div class="meter-copy">${esc(note)}</div>` : ""}
        </div>
      `;
    }

    function detailTab() {
      const raw = localStorage.getItem(storageKey("cfm_detail_tab")) || "overview";
      return ["overview", "ai", "structure", "liquidity"].includes(raw) ? raw : "overview";
    }

    function setDetailTab(tab) {
      localStorage.setItem(storageKey("cfm_detail_tab"), tab);
    }

    function clearDeferredDetailState() {
      detailInteractionUntil = 0;
      pendingDetailRefresh = false;
      pendingAIRefreshSymbol = null;
      if (detailInteractionTimer) {
        clearTimeout(detailInteractionTimer);
        detailInteractionTimer = null;
      }
    }

    function selectedSymbolSnapshot(symbols = lastSymbols) {
      return (symbols || []).find((item) => item.symbol === selectedSymbol) || null;
    }

    function detailRefreshLocked(symbol = selectedSymbol) {
      return Boolean(symbol && detailTab() === "ai" && Date.now() < detailInteractionUntil);
    }

    function scheduleDetailRefreshFlush() {
      if (detailInteractionTimer) clearTimeout(detailInteractionTimer);
      const delay = Math.max(DETAIL_INTERACTION_LOCK_MS, detailInteractionUntil - Date.now()) + 40;
      detailInteractionTimer = setTimeout(() => {
        detailInteractionTimer = null;
        flushDeferredDetailRefresh();
      }, delay);
    }

    function noteAIInteraction() {
      detailInteractionUntil = Date.now() + DETAIL_INTERACTION_LOCK_MS;
      scheduleDetailRefreshFlush();
    }

    function shouldDeferDetailRender(symbols) {
      if (!detailRefreshLocked()) return false;
      if ((detailEl.dataset.symbol || "") !== selectedSymbol) return false;
      return Boolean(selectedSymbolSnapshot(symbols));
    }

    function flushDeferredDetailRefresh() {
      if (detailRefreshLocked()) {
        scheduleDetailRefreshFlush();
        return;
      }
      if (!selectedSymbol) {
        pendingDetailRefresh = false;
        pendingAIRefreshSymbol = null;
        return;
      }
      if (pendingDetailRefresh) {
        pendingDetailRefresh = false;
        pendingAIRefreshSymbol = null;
        renderDetail(lastSymbols);
        return;
      }
      if (pendingAIRefreshSymbol && pendingAIRefreshSymbol === selectedSymbol && detailTab() === "ai") {
        const symbol = pendingAIRefreshSymbol;
        pendingAIRefreshSymbol = null;
        refreshAIBlock(symbol, false);
      }
    }

    function captureDetailScroll(symbol = selectedSymbol) {
      if (!symbol) return { drawer: 0, ai: 0, sameSymbol: false };
      const sameSymbol = (detailEl.dataset.symbol || "") === symbol;
      const aiBlock = document.getElementById("ai-block");
      const drawerTop = sideScrollEl ? sideScrollEl.scrollTop : (drawerScrollBySymbol[symbol] || 0);
      const aiTop = aiBlock ? aiBlock.scrollTop : (aiScrollBySymbol[symbol] || 0);
      drawerScrollBySymbol[symbol] = drawerTop;
      aiScrollBySymbol[symbol] = aiTop;
      return { drawer: drawerTop, ai: aiTop, sameSymbol };
    }

    function applyScrollRestore(element, top) {
      if (!element) return;
      const nextTop = Math.max(0, Number(top) || 0);
      if (Math.abs(element.scrollTop - nextTop) < 1) return;
      element.scrollTop = nextTop;
      requestAnimationFrame(() => {
        if (element && element.isConnected && Math.abs(element.scrollTop - nextTop) >= 1) {
          element.scrollTop = nextTop;
        }
      });
    }

    function restoreDetailScroll(symbol, preserved, activeTab) {
      requestAnimationFrame(() => {
        if (sideScrollEl) {
          const drawerTop = preserved && preserved.sameSymbol
            ? preserved.drawer
            : (drawerScrollBySymbol[symbol] || 0);
          applyScrollRestore(sideScrollEl, drawerTop);
        }
        const aiBlock = document.getElementById("ai-block");
        if (aiBlock && activeTab === "ai") {
          const aiTop = preserved && preserved.sameSymbol
            ? preserved.ai
            : (aiScrollBySymbol[symbol] || 0);
          applyScrollRestore(aiBlock, aiTop);
        }
      });
    }

    function refreshAIBlock(symbol, deferIfLocked = true) {
      const aiBlock = document.getElementById("ai-block");
      if (!aiBlock) return;
      const scrollTop = aiBlock.scrollTop;
      aiScrollBySymbol[symbol] = scrollTop;
      if (deferIfLocked && selectedSymbol === symbol && detailRefreshLocked(symbol)) {
        pendingAIRefreshSymbol = symbol;
        scheduleDetailRefreshFlush();
        return;
      }
      pendingAIRefreshSymbol = null;
      aiBlock.innerHTML = renderAIBlock(symbol);
      const nextBlock = document.getElementById("ai-block");
      applyScrollRestore(nextBlock, aiScrollBySymbol[symbol] || 0);
    }

    function structureMarkerHtml(marker) {
      const classes = ["range-rail-marker", marker.tone || "muted", `lane-${marker.lane || 0}`];
      if (marker.compact) classes.push("compact");
      const shift = Number(marker.shift || 0);
      if (shift < 0) classes.push("has-shift", "shift-left");
      if (shift > 0) classes.push("has-shift", "shift-right");
      return `
        <div class="${classes.join(" ")}" style="left:${marker.left.toFixed(2)}%; --label-offset:${shift.toFixed(0)}px; --leader-width:${Math.abs(shift).toFixed(0)}px">
          <strong>${esc(marker.label)}</strong>
          <span class="marker-leader"></span>
          <span class="marker-stem"></span>
        </div>
      `;
    }

    function structureMarkerPriority(label) {
      if (label === "支撑") return 0;
      if (label === "VWAP") return 1;
      if (label === "现价") return 2;
      if (label === "压力") return 3;
      return 4;
    }

    function sortStructureMarkers(left, right) {
      const diff = Number(left.left || 0) - Number(right.left || 0);
      if (Math.abs(diff) >= 0.01) return diff;
      return structureMarkerPriority(left.label) - structureMarkerPriority(right.label);
    }

    function applyStructureCluster(cluster) {
      if (!cluster.length) return;
      const averageLeft = cluster.reduce((sum, marker) => sum + Number(marker.left || 0), 0) / cluster.length;
      const compact = cluster.length >= 3;
      const step = compact ? 54 : 42;
      cluster.forEach((marker) => {
        marker.compact = compact;
        marker.lane = 0;
        marker.shift = 0;
      });

      if (averageLeft >= 82) {
        const ordered = [...cluster].sort((left, right) => {
          const diff = Number(right.left || 0) - Number(left.left || 0);
          if (Math.abs(diff) >= 0.01) return diff;
          return structureMarkerPriority(right.label) - structureMarkerPriority(left.label);
        });
        ordered.forEach((marker, index) => {
          marker.shift = -(24 + index * step);
        });
        return;
      }

      if (averageLeft <= 18) {
        const ordered = [...cluster].sort(sortStructureMarkers);
        ordered.forEach((marker, index) => {
          marker.shift = 24 + index * step;
        });
        return;
      }

      const ordered = [...cluster].sort(sortStructureMarkers);
      const centerIndex = (ordered.length - 1) / 2;
      ordered.forEach((marker, index) => {
        marker.shift = Math.round((index - centerIndex) * step);
      });
    }

    function structureMarkers(symbol, rangePos, vwapPos) {
      const markers = [
        { label: "支撑", left: 0, tone: "up", lane: 0 },
        { label: "VWAP", left: vwapPos, tone: "mixed", lane: 0 },
        { label: "现价", left: rangePos, tone: valueClass(symbol.price_move_pct_1m), lane: 0 },
        { label: "压力", left: 100, tone: "down", lane: 0 }
      ];
      const closenessThreshold = 9;
      markers.forEach((marker) => {
        if (marker.left <= 6) marker.shift = 24;
        if (marker.left >= 94) marker.shift = -24;
      });
      const sorted = [...markers].sort(sortStructureMarkers);
      const clusters = [];
      sorted.forEach((marker) => {
        const current = clusters[clusters.length - 1];
        if (!current || Math.abs(marker.left - current[current.length - 1].left) >= closenessThreshold) {
          clusters.push([marker]);
          return;
        }
        current.push(marker);
      });
      clusters.filter((cluster) => cluster.length > 1).forEach(applyStructureCluster);
      return markers.map((marker) => structureMarkerHtml(marker)).join("");
    }

    function fmtPct(value) {
      const number = Number(value || 0);
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(3)}%</span>`;
    }

    function fmtFunding(value) {
      const number = Number(value || 0) * 100;
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(4)}%</span>`;
    }

    function fmtBps(value) {
      const number = Number(value || 0);
      const cls = number >= 4 ? "down" : number >= 2 ? "mixed" : "muted";
      return `<span class="${cls}">${number.toFixed(2)}</span>`;
    }

    function fmtPriceLevel(value) {
      const number = Number(value || 0);
      if (!number) return "--";
      return fmtNumber(number, 8);
    }

    function fmtPlainPct(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      return `${Number(value).toFixed(digits)}%`;
    }

    function liquidationStatusText(symbol) {
      const status = symbol.liquidation_data_status || "unavailable";
      if (status === "recent_event") return "有强平";
      if (status === "no_recent_event") return "近1m无";
      return "未接入";
    }

    function liquidationStatusClass(symbol) {
      const status = symbol.liquidation_data_status || "unavailable";
      if (status === "recent_event") return "down";
      if (status === "no_recent_event") return "muted";
      return "mixed";
    }

    function liquidationTotalHtml(symbol) {
      if ((symbol.liquidation_data_status || "unavailable") === "unavailable") {
        return `<span class="mixed">未接入</span>`;
      }
      if ((symbol.liquidation_data_status || "") === "no_recent_event") {
        return `<span class="muted">近1m无</span>`;
      }
      return `<span class="down">${fmtNumber(symbol.liquidation_total_quote_1m, 0)}</span>`;
    }

    function liquidationSideHtml(symbol, key) {
      if ((symbol.liquidation_data_status || "unavailable") === "unavailable") {
        return `<span class="mixed">未接入</span>`;
      }
      return fmtNumber(symbol[key], 0);
    }

    function microstructureStatusHtml(symbol) {
      if ((symbol.microstructure_status || "unavailable") === "active") {
        return `<span class="up">流活跃</span>`;
      }
      return `<span class="mixed">未接入</span>`;
    }

    function riskClass(level) {
      if (level && level.includes("高")) return "risk-high";
      if (level && level.includes("中")) return "risk-mid";
      return "risk-low";
    }

    function shortBias(bias) {
      const text = bias || "观察";
      if (text.includes("偏多")) return "偏多";
      if (text.includes("偏空")) return "偏空";
      if (text.includes("拥挤")) return "拥挤";
      if (text.includes("波动")) return "波动";
      return "观察";
    }

    function biasClass(bias) {
      const text = shortBias(bias);
      if (text === "偏多") return "bias-up";
      if (text === "偏空") return "bias-down";
      if (text === "拥挤" || text === "波动") return "bias-crowded";
      return "bias-watch";
    }

    function signalTag(symbol) {
      if (Number(symbol.score || 0) >= 70) return "报警";
      if (Number(symbol.score || 0) >= 45) return "关注";
      if (Math.abs(Number(symbol.price_move_pct_1m || 0)) >= 0.8) return "急动";
      if (Number(symbol.volume_multiplier || 0) >= 3) return "放量";
      if (Math.abs(Number(symbol.oi_change_pct_5m || 0)) >= 0.3) return "OI";
      if ((symbol.liquidation_data_status || "") === "recent_event" && Number(symbol.liquidation_total_quote_1m || 0) >= 250000) return "爆仓";
      if (Number(symbol.spread_bps || 0) >= 4 || Number(symbol.depth_drop_pct_1m || 0) >= 18) return "盘口";
      if (Math.abs(Number(symbol.funding_rate || 0)) >= 0.0005) return "费率";
      if (Number(symbol.score || 0) > 0) return "监测";
      return "静默";
    }

    function rowClass(symbol) {
      if (symbol.direction === "up") return "up";
      if (symbol.direction === "down") return "down";
      if (symbol.direction === "mixed") return "mixed";
      return "muted";
    }

    function valueClass(value) {
      const number = Number(value || 0);
      if (number > 0) return "up";
      if (number < 0) return "down";
      return "muted";
    }

    function currentThresholdText(symbol) {
      const config = symbolThresholds[symbol] || {};
      const hasRules = hasEnabledThresholdRules(config.push_rules);
      if (hasRules && config.anomaly_score !== undefined && config.anomaly_score !== null) {
        return `规则 + ${fmtNumber(config.anomaly_score, 1)} 分`;
      }
      if (hasRules) {
        return "组合规则";
      }
      if (config && config.anomaly_score !== undefined && config.anomaly_score !== null) {
        return `${fmtNumber(config.anomaly_score, 1)} 分`;
      }
      return `全局 ${fmtNumber(globalThreshold, 1)} 分`;
    }

    function thresholdButtonText(symbol) {
      const config = symbolThresholds[symbol] || {};
      const hasRules = hasEnabledThresholdRules(config.push_rules);
      const hasScore = config.anomaly_score !== undefined && config.anomaly_score !== null;
      if (hasRules && hasScore) return "自定义";
      if (hasRules) return "组合";
      if (hasScore) return "分数";
      return "规则";
    }

    function structureState(symbol) {
      const supportDistance = Number(symbol.support_distance_pct || 0);
      const resistanceDistance = Number(symbol.resistance_distance_pct || 0);
      const rangePosition = Number(symbol.range_position_pct || 50);
      const vwapDeviation = Number(symbol.vwap_deviation_pct || 0);
      if (supportDistance <= 0.12 && resistanceDistance <= 0.12) return "区间极窄";
      if (supportDistance <= 0.18 && supportDistance <= resistanceDistance) return "贴近支撑";
      if (resistanceDistance <= 0.18 && resistanceDistance < supportDistance) return "贴近压力";
      if (rangePosition >= 80) return "区间上沿";
      if (rangePosition <= 20) return "区间下沿";
      if (vwapDeviation >= 0.35) return "高于 VWAP";
      if (vwapDeviation <= -0.35) return "低于 VWAP";
      return "区间中部";
    }

    function structureMeta(symbol) {
      const supportDistance = fmtPlainPct(symbol.support_distance_pct, 2);
      const resistanceDistance = fmtPlainPct(symbol.resistance_distance_pct, 2);
      return `距撑 ${supportDistance} / 距压 ${resistanceDistance}`;
    }

    function structureNarrative(symbol) {
      const state = structureState(symbol);
      const support = fmtPriceLevel(symbol.support_price);
      const resistance = fmtPriceLevel(symbol.resistance_price);
      const supportDistance = fmtPlainPct(symbol.support_distance_pct, 2);
      const resistanceDistance = fmtPlainPct(symbol.resistance_distance_pct, 2);
      const bidWall = fmtPriceLevel(symbol.bid_wall_price);
      const askWall = fmtPriceLevel(symbol.ask_wall_price);
      if (state === "区间极窄") {
        return `价格挤在 ${support} - ${resistance} 的窄区间里，短线更容易先走假突破，先等放量确认。`;
      }
      if (state === "贴近支撑") {
        return `当前更靠近支撑 ${support}，距支撑约 ${supportDistance}；若买盘墙 ${bidWall} 继续承接，短线更利于反抽观察。`;
      }
      if (state === "贴近压力") {
        return `当前更靠近压力 ${resistance}，距压力约 ${resistanceDistance}；若卖盘墙 ${askWall} 持续压制，短线更容易先遇阻。`;
      }
      if (state === "区间上沿") {
        return `价格运行在区间上半段，离压力更近，重点看是否放量站上 ${resistance}。`;
      }
      if (state === "区间下沿") {
        return `价格运行在区间下半段，离支撑更近，重点看 ${support} 是否继续被动承接。`;
      }
      if (state === "高于 VWAP") {
        return `当前价格在区间 VWAP 上方，说明短线均价偏强，但若无法继续抬高压力位，容易回归均价。`;
      }
      if (state === "低于 VWAP") {
        return `当前价格在区间 VWAP 下方，说明短线均价偏弱，除非快速收回 VWAP，否则更偏震荡偏弱。`;
      }
      return `当前处在区间中部，支撑 ${support} 与压力 ${resistance} 都还有效，优先观察哪一侧先被放量突破。`;
    }

    function flowNarrative(symbol) {
      const buyRatio = Number(symbol.taker_buy_ratio_1m || 0) * 100;
      const depthDrop = Number(symbol.depth_drop_pct_1m || 0);
      const imbalance = Number(symbol.depth_imbalance || 0) * 100;
      const spread = Number(symbol.spread_bps || 0);
      if (depthDrop >= 18 && spread >= 4) {
        return `盘口明显变薄，点差 ${spread.toFixed(2)} bps，当前更要防插针和瞬时滑点。`;
      }
      if (buyRatio >= 65 && imbalance >= 10) {
        return `主动买入和买盘深度都偏强，若价格还能站稳 VWAP，上冲延续性会更好。`;
      }
      if (buyRatio <= 35 && imbalance <= -10) {
        return `主动卖出与卖盘深度都偏强，除非快速收回均价，否则下压更占优。`;
      }
      return `当前流向没有形成极端单边，先把盘口墙、VWAP 和区间边界一起看。`;
    }

    function structureStateClass(state) {
      if (state.includes("支撑")) return "up";
      if (state.includes("压力")) return "down";
      if (state.includes("上沿") || state.includes("下沿") || state.includes("窄")) return "mixed";
      return "muted";
    }

    function hasEnabledThresholdRules(rules) {
      const conditions = rules && rules.conditions ? rules.conditions : {};
      return Object.values(conditions).some((cfg) => Boolean(cfg && cfg.enabled));
    }

    function applyThresholdRuleForm(rules, fallbackScore = globalThreshold) {
      const conditions = rules && rules.conditions ? rules.conditions : {};
      thresholdTriggerMode.value = rules && rules.mode === "all" ? "all" : "any";
      Object.entries(thresholdTriggerFields).forEach(([key, fields]) => {
        const cfg = conditions[key] || {};
        fields[0].checked = Boolean(cfg.enabled);
        const defaultValue = key === "score" ? fallbackScore : fields[1].value;
        fields[1].value = cfg.threshold ?? defaultValue;
      });
    }

    function collectThresholdRules() {
      const conditions = {};
      Object.entries(thresholdTriggerFields).forEach(([key, fields]) => {
        conditions[key] = {
          enabled: fields[0].checked,
          threshold: Number(fields[1].value) || 0
        };
      });
      return { mode: thresholdTriggerMode.value || "any", conditions };
    }

    function aiStatusLine(symbol) {
      const meta = aiMeta[symbol];
      if (!meta) return "";
      const timeText = meta.ts ? new Date(meta.ts).toLocaleTimeString() : "";
      return `<div class="ai-status"><strong>${esc(meta.status)}</strong><span>${esc(timeText)}</span></div>`;
    }

    function aiSections(text) {
      const lines = String(text || "").split("\\n").map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return [];
      const sections = [];
      let current = null;

      function flushCurrent() {
        if (!current) return;
        const body = current.bodyLines.join(" ").trim();
        sections.push({ title: current.title, body: body || "等待补充内容" });
        current = null;
      }

      lines.forEach((line) => {
        const markdownHeading = line.match(/^(?:\\d+[\\.、]\\s*)?\\*\\*(.+?)\\*\\*[:：]?\\s*(.*)$/);
        if (markdownHeading) {
          flushCurrent();
          current = { title: markdownHeading[1], bodyLines: [] };
          if (markdownHeading[2]) current.bodyLines.push(markdownHeading[2]);
          return;
        }

        const titledLine = line.match(/^(?:\\d+[\\.、]\\s*)?([^:：]{2,20})[:：]\\s*(.+)$/);
        if (titledLine) {
          flushCurrent();
          sections.push({ title: titledLine[1], body: titledLine[2] });
          return;
        }

        const numberedLine = line.match(/^(\\d+)[\\.、]\\s*(.+)$/);
        if (numberedLine) {
          flushCurrent();
          sections.push({ title: `要点 ${numberedLine[1]}`, body: numberedLine[2] });
          return;
        }

        if (current) {
          current.bodyLines.push(line);
          return;
        }

        sections.push({ title: sections.length ? `补充 ${sections.length + 1}` : "AI 摘要", body: line });
      });

      flushCurrent();
      return sections;
    }

    function renderAIBlock(symbol) {
      const text = aiResults[symbol];
      if (!text) {
        return `${aiStatusLine(symbol)}<div class="detail-placeholder">等待 AI 根据当前合约指标生成观察建议。</div>`;
      }
      const sections = aiSections(text);
      const cardsHtml = sections.length
        ? `<div class="ai-grid">${sections.map((item) => `
            <div class="ai-card">
              <div class="ai-card-title">${esc(item.title)}</div>
              <div class="ai-card-copy">${esc(item.body)}</div>
            </div>
          `).join("")}</div>`
        : `<div class="detail-placeholder">AI 返回了空内容，请稍后重试。</div>`;
      return aiStatusLine(symbol) + cardsHtml;
    }

    function detailTabButton(tab, label) {
      return `<button class="detail-tab ${detailTab() === tab ? "active" : ""}" data-detail-tab="${esc(tab)}" type="button">${esc(label)}</button>`;
    }

    function insightCard(kicker, value, copy, tone = "muted") {
      return `
        <div class="insight-card">
          <div class="insight-kicker">${esc(kicker)}</div>
          <div class="insight-value ${esc(tone)}">${esc(value)}</div>
          <div class="insight-copy">${esc(copy)}</div>
        </div>
      `;
    }

    function chartCard(title, meta, body, extraClass = "") {
      return `
        <div class="chart-card ${extraClass}">
          <div class="chart-head">
            <div class="chart-title">${esc(title)}</div>
            <div class="chart-meta">${meta}</div>
          </div>
          ${body}
        </div>
      `;
    }

    function renderOverviewPanel(symbol) {
      const tradeLive = hasCoreTradeData(symbol);
      const oiLive = hasOiData(symbol);
      const depthLive = hasDepthData(symbol);
      const structureLive = hasStructureData(symbol);
      const reasons = (symbol.reasons || []).length ? symbol.reasons : ["暂无明确触发项"];
      const structureText = structureLive ? structureNarrative(symbol) : "等待成交数据后补齐结构判断。";
      const flowText = depthLive ? flowNarrative(symbol) : "盘口深度暂未到位，先以价格与量能为主。";
      const liquidationText = (symbol.liquidation_data_status || "unavailable") === "recent_event"
        ? `近 1 分钟爆仓 ${fmtNumber(symbol.liquidation_total_quote_1m, 0)} USDT`
        : (symbol.liquidation_data_status || "unavailable") === "no_recent_event"
          ? "近 1 分钟暂无可识别爆仓"
          : "当前数据源未给到有效爆仓流";
      return `
        <div class="detail-panel">
          <div class="insight-grid">
            ${insightCard("结构", structureLive ? structureState(symbol) : "等待结构", structureText, structureLive ? structureStateClass(structureState(symbol)) : "muted")}
            ${insightCard("盘口", shortBias(symbol.bias || "观察"), flowText, rowClass(symbol))}
            ${insightCard("爆仓", liquidationStatusText(symbol), liquidationText, liquidationStatusClass(symbol))}
          </div>
          <div>
            <div class="detail-title">本次关注点</div>
            <div class="reason-pills">
              ${reasons.map((item) => `<span class="reason-pill">${esc(item)}</span>`).join("")}
            </div>
          </div>
        </div>
      `;
    }

    function renderStructurePanel(symbol) {
      const available = hasStructureData(symbol);
      if (!available) {
        return `<div class="detail-panel"><div class="detail-placeholder">等待成交序列稳定后，再展示支撑、压力、VWAP 与区间位置。</div></div>`;
      }
      const support = Number(symbol.support_price || 0);
      const resistance = Number(symbol.resistance_price || 0);
      const price = Number(symbol.price || 0);
      const vwap = Number(symbol.window_vwap || 0);
      const spread = Math.max(resistance - support, 1e-9);
      const rangePos = clamp(Number(symbol.range_position_pct || 50), 0, 100);
      const vwapPos = clamp(((vwap - support) / spread) * 100, 0, 100);
      return `
        <div class="detail-panel">
          <div class="range-rail-card">
            <div class="range-rail-head">
              <div class="range-rail-title">支撑 / 压力结构带</div>
              <div class="range-rail-meta">${esc(structureState(symbol))}</div>
            </div>
            <div class="range-rail">
              <div class="range-rail-track"></div>
              ${structureMarkers(symbol, rangePos, vwapPos)}
            </div>
            <div class="range-rail-footer">
              <div class="rail-stat">
                <div class="rail-stat-label">支撑位</div>
                <div class="rail-stat-value">${fmtPriceLevel(support)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">现价</div>
                <div class="rail-stat-value">${fmtPriceLevel(price)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">VWAP</div>
                <div class="rail-stat-value">${fmtPriceLevel(vwap)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">压力位</div>
                <div class="rail-stat-value">${fmtPriceLevel(resistance)}</div>
              </div>
            </div>
          </div>
          <div class="metric-grid compact">
            <div class="metric">
              <div class="metric-label">距支撑</div>
              <div class="metric-value up">${fmtPlainPct(symbol.support_distance_pct, 2)}</div>
              <div class="metric-copy">越短说明越接近下沿承接区</div>
            </div>
            <div class="metric">
              <div class="metric-label">距压力</div>
              <div class="metric-value down">${fmtPlainPct(symbol.resistance_distance_pct, 2)}</div>
              <div class="metric-copy">越短说明越接近上沿抛压区</div>
            </div>
            <div class="metric">
              <div class="metric-label">区间位置</div>
              <div class="metric-value">${fmtPlainPct(symbol.range_position_pct, 2)}</div>
              <div class="metric-copy">${esc(structureState(symbol))}</div>
            </div>
            <div class="metric">
              <div class="metric-label">VWAP 偏离</div>
              <div class="metric-value ${valueClass(symbol.vwap_deviation_pct)}">${fmtPlainPct(symbol.vwap_deviation_pct, 3)}</div>
              <div class="metric-copy">短线均价偏离度</div>
            </div>
            <div class="metric">
              <div class="metric-label">买盘墙</div>
              <div class="metric-value">${fmtPriceLevel(symbol.bid_wall_price)}</div>
              <div class="metric-copy">金额 ${fmtNumber(symbol.bid_wall_notional, 0)}</div>
            </div>
            <div class="metric">
              <div class="metric-label">卖盘墙</div>
              <div class="metric-value">${fmtPriceLevel(symbol.ask_wall_price)}</div>
              <div class="metric-copy">金额 ${fmtNumber(symbol.ask_wall_notional, 0)}</div>
            </div>
          </div>
        </div>
      `;
    }

    function renderLiquidityPanel(symbol) {
      const depthLive = hasDepthData(symbol);
      const tradeLive = hasCoreTradeData(symbol);
      const totalDepth = Number(symbol.bid_depth_notional || 0) + Number(symbol.ask_depth_notional || 0);
      const bidPct = totalDepth > 0 ? (Number(symbol.bid_depth_notional || 0) / totalDepth) * 100 : 50;
      const askPct = 100 - bidPct;
      return `
        <div class="detail-panel">
          ${depthLive ? `
            <div class="depth-card">
              <div class="chart-head">
                <div class="chart-title">买卖盘深度对比</div>
                <div class="chart-meta">${fmtBps(symbol.spread_bps)} bps</div>
              </div>
              <div class="depth-split">
                <div class="depth-track">
                  <span style="width:${bidPct.toFixed(1)}%"></span>
                  <span style="width:${askPct.toFixed(1)}%"></span>
                </div>
                <div class="depth-meta">
                  <span>买盘 ${fmtNumber(symbol.bid_depth_notional, 0)}</span>
                  <span>卖盘 ${fmtNumber(symbol.ask_depth_notional, 0)}</span>
                </div>
              </div>
            </div>
          ` : `<div class="detail-placeholder">当前数据源没有稳定盘口深度，流动性看板先保持静默。</div>`}
          <div class="metric-grid compact">
            <div class="metric"><div class="metric-label">强平状态</div><div class="metric-value ${liquidationStatusClass(symbol)}">${esc(liquidationStatusText(symbol))}</div><div class="metric-copy">总额 ${liquidationTotalHtml(symbol)}</div></div>
            <div class="metric"><div class="metric-label">强平事件 1m</div><div class="metric-value">${fmtNumber(symbol.liquidation_event_count_1m, 0)}</div><div class="metric-copy">近一分钟记录数</div></div>
            <div class="metric"><div class="metric-label">多头爆仓</div><div class="metric-value">${liquidationSideHtml(symbol, "long_liquidation_quote_1m")}</div><div class="metric-copy">USDT</div></div>
            <div class="metric"><div class="metric-label">空头爆仓</div><div class="metric-value">${liquidationSideHtml(symbol, "short_liquidation_quote_1m")}</div><div class="metric-copy">USDT</div></div>
            <div class="metric"><div class="metric-label">盘口点差</div><div class="metric-value">${depthLive ? `${fmtBps(symbol.spread_bps)} bps` : mutedValue()}</div><div class="metric-copy">越大越易滑点</div></div>
            <div class="metric"><div class="metric-label">深度下降</div><div class="metric-value">${depthLive ? `${fmtNumber(symbol.depth_drop_pct_1m, 1)}%` : mutedValue()}</div><div class="metric-copy">越快越易插针</div></div>
            <div class="metric"><div class="metric-label">盘口失衡</div><div class="metric-value">${depthLive ? `${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">正值偏买盘，负值偏卖盘</div></div>
            <div class="metric"><div class="metric-label">主动买入</div><div class="metric-value">${tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">成交主导方向</div></div>
          </div>
        </div>
      `;
    }

    function eventLevel(event) {
      const score = Number(event.score || 0);
      if (score >= 70) return "风险预警";
      if (score >= 45) return "关注信号";
      return "观察信号";
    }

    function eventDirectionClass(event) {
      if (event.direction === "up") return "up";
      if (event.direction === "down") return "down";
      return "mixed";
    }

    function openModal(modal) {
      modal.classList.add("open");
    }

    function closeModal(modal) {
      modal.classList.remove("open");
    }

    function readCollapseState() {
      try {
        return JSON.parse(localStorage.getItem(storageKey("cfm_collapsed_sections")) || "{}");
      } catch (error) {
        return {};
      }
    }

    const defaultCollapsedSections = {
      detail_micro: true,
      detail_reasons: true,
      events: false,
    };

    function isCollapsed(section) {
      const state = readCollapseState();
      if (Object.prototype.hasOwnProperty.call(state, section)) {
        return Boolean(state[section]);
      }
      return Boolean(defaultCollapsedSections[section]);
    }

    function setCollapsed(section, collapsed) {
      const state = readCollapseState();
      state[section] = Boolean(collapsed);
      localStorage.setItem(storageKey("cfm_collapsed_sections"), JSON.stringify(state));
    }

    function updateCollapseButton(button, collapsed) {
      if (!button) return;
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const icon = button.querySelector(".collapse-icon");
      if (icon) icon.textContent = collapsed ? "+" : "-";
    }

    function collapseClass(section) {
      return isCollapsed(section) ? " collapsed" : "";
    }

    function collapseHead(section, title, meta = "") {
      const collapsed = isCollapsed(section);
      const metaHtml = meta ? `<span class="collapse-meta">${esc(meta)}</span>` : "";
      return `
        <button class="collapse-head" data-collapse="${esc(section)}" type="button" aria-expanded="${collapsed ? "false" : "true"}">
          <span class="collapse-main">
            <span class="collapse-title">${esc(title)}</span>
            ${metaHtml}
          </span>
          <span class="collapse-icon">${collapsed ? "+" : "-"}</span>
        </button>
      `;
    }

    function applyEventsCollapseState() {
      const collapsed = isCollapsed("events");
      eventsEl.classList.toggle("collapsed-list", collapsed);
      updateCollapseButton(eventsCollapseBtn, collapsed);
    }

    function renderMetricSummary(symbol, extraClass = "") {
      const tradeLive = hasCoreTradeData(symbol);
      const oiLive = hasOiData(symbol);
      const depthLive = hasDepthData(symbol);
      const structureLive = hasStructureData(symbol);
      const items = [
        ["风险", `<span class="${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</span>`],
        ["置信度", tradeLive ? `${fmtNumber(symbol.confidence, 1)}%` : mutedValue()],
        ["1m波动", fmtPctMaybe(symbol.price_move_pct_1m, tradeLive)],
        ["1m成交额", fmtNumberMaybe(symbol.quote_volume_1m, 0, tradeLive)],
        ["量能", tradeLive ? `<span class="${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</span>` : mutedValue()],
        ["OI 5m", fmtPctMaybe(symbol.oi_change_pct_5m, oiLive)],
        ["主动买入", tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : mutedValue()],
        ["结构", structureLive ? `<span class="${structureStateClass(structureState(symbol))}">${esc(structureState(symbol))}</span>` : mutedValue("等待结构")],
        ["爆仓", `<span class="${liquidationStatusClass(symbol)}">${esc(liquidationStatusText(symbol))}</span>`],
        ["点差", depthLive ? `${fmtBps(symbol.spread_bps)} bps` : mutedValue()]
      ];
      return `
        <div class="metric-summary ${extraClass}">
          ${items.map(([label, value]) => `
            <div class="summary-chip">
              <span class="summary-label">${esc(label)}</span>
              <span class="summary-value">${value}</span>
            </div>
          `).join("")}
        </div>
      `;
    }

    function renderSymbols(symbols) {
      lastSymbols = symbols || [];
      countEl.textContent = `${symbols.length} 个合约`;
      if (selectedSymbol && !symbols.some((symbol) => symbol.symbol === selectedSymbol)) {
        selectedSymbol = symbols[0] ? symbols[0].symbol : null;
        if (!selectedSymbol) setDrawerOpen(false);
      }
      if (!inputTouched) {
        symbolInputEl.value = symbols.map((symbol) => symbol.symbol).join(", ");
      }

      symbolsEl.innerHTML = symbols.map((symbol) => `
        <tr data-symbol="${esc(symbol.symbol)}" class="${symbol.symbol === selectedSymbol ? "selected" : ""}">
          <td>
            <div class="symbol">${esc(symbol.symbol)}</div>
            <div class="symbol-meta">
              <span class="sub-pill">${esc(signalTag(symbol))}</span>
              <button class="row-action-btn js-threshold ${thresholdButtonText(symbol.symbol) !== "规则" ? "active" : ""}" data-symbol="${esc(symbol.symbol)}" type="button" title="${esc(currentThresholdText(symbol.symbol))}">${esc(thresholdButtonText(symbol.symbol))}</button>
            </div>
          </td>
          <td><span class="score">${fmtNumber(symbol.score, 1)}</span></td>
          <td><span class="risk ${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</span></td>
          <td><span class="tag ${biasClass(symbol.bias)}">${esc(shortBias(symbol.bias))}</span></td>
          <td>${fmtNumber(symbol.price, 8)}</td>
          <td>${fmtPct(symbol.price_move_pct_1m)}</td>
          <td>${fmtPct(symbol.price_move_pct_5m)}</td>
          <td>${fmtNumber(symbol.quote_volume_1m, 0)}</td>
          <td class="${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</td>
          <td>${fmtPct(symbol.oi_change_pct_5m)}</td>
          <td>${liquidationTotalHtml(symbol)}</td>
          <td>${fmtBps(symbol.spread_bps)}</td>
        </tr>
      `).join("");

      symbolsEl.querySelectorAll("tr").forEach((row) => {
        row.addEventListener("click", () => {
          selectedSymbol = row.dataset.symbol;
          openDetailDrawer(selectedSymbol);
          renderSymbols(symbols);
          renderDetail(symbols);
        });
      });
      symbolsEl.querySelectorAll(".js-threshold").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          selectedSymbol = button.dataset.symbol;
          openDetailDrawer(selectedSymbol);
          renderSymbols(symbols);
          renderDetail(symbols);
          openThresholdModal(selectedSymbol);
        });
      });
    }

    function renderDetail(symbols) {
      const symbol = symbols.find((item) => item.symbol === selectedSymbol);
      if (!symbol) {
        delete detailEl.dataset.symbol;
        detailEl.innerHTML = `
          <div class="drawer-empty">
            <div class="drawer-empty-title">点击左侧合约展开详情</div>
            <div class="drawer-empty-copy">这里会先看 5 分钟价格、量能、OI 图，再切到 AI、结构位和流动性视图。</div>
          </div>
        `;
        return;
      }

      selectedSymbol = symbol.symbol;
      const preservedScroll = captureDetailScroll(symbol.symbol);
      const tradeLive = hasCoreTradeData(symbol);
      const depthLive = hasDepthData(symbol);
      const oiLive = hasOiData(symbol);
      const activeTab = detailTab();
      const priceChart = tradeLive
        ? lineChartSvg(symbol.price_series_5m, valueClass(symbol.price_move_pct_5m))
        : chartEmptyHtml("等待成交时序");
      const volumeChart = tradeLive
        ? barChartSvg(symbol.volume_series_5m, rowClass(symbol))
        : chartEmptyHtml("等待成交时序");
      const oiTone = Number(symbol.oi_change_pct_5m || 0) > 0 ? "up" : Number(symbol.oi_change_pct_5m || 0) < 0 ? "down" : "mixed";
      const oiChart = oiLive
        ? lineChartSvg(symbol.oi_series_5m, oiTone)
        : chartEmptyHtml("OI 尚未稳定");
      const depthBalance = clamp((Number(symbol.depth_imbalance || 0) + 1) * 50, 0, 100);
      maybeLoadAIAnalysis(symbol);
      detailEl.innerHTML = `
        <div class="detail-head">
          <div class="detail-meta">
            <div class="detail-title-row">
              <div class="detail-symbol">${esc(symbol.symbol)}</div>
              <div class="detail-tools">
                <span class="risk ${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</span>
                <span class="tag ${biasClass(symbol.bias)}">${esc(shortBias(symbol.bias || ""))}</span>
              </div>
            </div>
            <div class="detail-price-wrap">
              <div class="detail-price-label">当前价格</div>
              <div class="detail-price ${valueClass(symbol.price_move_pct_1m)}">${tradeLive ? fmtNumber(symbol.price, 8) : "--"}</div>
            </div>
            <div class="detail-bias">${esc(symbol.bias || "观察：暂无明确方向")}</div>
          </div>
          <div>
            <span class="score">${fmtNumber(symbol.score, 1)}</span>
            <div class="cell-sub" style="margin-top:8px;text-align:right">置信度 ${tradeLive ? `${fmtNumber(symbol.confidence, 1)}%` : "--"}</div>
          </div>
        </div>
        ${dataBannerHtml(symbol)}
        ${renderMetricSummary(symbol, "always-visible")}
        <div class="visual-grid">
          ${chartCard("5分钟价格轨迹", tradeLive ? fmtPctMaybe(symbol.price_move_pct_5m, true) : mutedValue(), priceChart, "span-2")}
          ${chartCard("1分钟量能", tradeLive ? `${fmtNumber(symbol.quote_volume_1m, 0)} USDT` : mutedValue(), volumeChart)}
          ${chartCard("5分钟 OI", oiLive ? fmtPctMaybe(symbol.oi_change_pct_5m, true) : mutedValue(), oiChart)}
        </div>
        <div class="meter-grid">
          ${meterHtml("主动买入", tradeLive ? Number(symbol.taker_buy_ratio_1m || 0) * 100 : 0, tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : "--", Number(symbol.taker_buy_ratio_1m || 0) >= 0.6 ? "up" : Number(symbol.taker_buy_ratio_1m || 0) <= 0.4 ? "down" : "mixed", "看主动成交方向是否持续。")}
          ${meterHtml("区间位置", tradeLive ? Number(symbol.range_position_pct || 50) : 0, tradeLive ? fmtPlainPct(symbol.range_position_pct, 1) : "--", Number(symbol.range_position_pct || 50) >= 70 ? "down" : Number(symbol.range_position_pct || 50) <= 30 ? "up" : "mixed", "越靠上沿越接近抛压区。")}
          ${meterHtml("盘口失衡", depthLive ? depthBalance : 0, depthLive ? `${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%` : "--", Number(symbol.depth_imbalance || 0) >= 0.1 ? "up" : Number(symbol.depth_imbalance || 0) <= -0.1 ? "down" : "mixed", "正值偏买盘，负值偏卖盘。")}
          ${meterHtml("量能倍数", tradeLive ? clamp((Number(symbol.volume_multiplier || 0) / 5) * 100, 0, 100) : 0, tradeLive ? `${fmtNumber(symbol.volume_multiplier, 2)}x` : "--", Number(symbol.volume_multiplier || 0) >= 3 ? "up" : Number(symbol.volume_multiplier || 0) >= 1.5 ? "mixed" : "blue", "5x 以上视作极强放量。")}
        </div>
        <div class="detail-tabs">
          ${detailTabButton("overview", "总览")}
          ${detailTabButton("ai", "AI 分析")}
          ${detailTabButton("structure", "结构位")}
          ${detailTabButton("liquidity", "流动性")}
        </div>
        ${activeTab === "ai" ? `
          <div class="detail-panel">
            <div class="ai-panel-head">
              <div class="detail-title">AI 只在触发条件满足或手动刷新时更新</div>
              <button class="inline-link" id="ai-refresh-btn" type="button">刷新</button>
            </div>
            <div class="detail-list ai-inline" id="ai-block">${renderAIBlock(symbol.symbol)}</div>
          </div>
        ` : ""}
        ${activeTab === "overview" ? renderOverviewPanel(symbol) : ""}
        ${activeTab === "structure" ? renderStructurePanel(symbol) : ""}
        ${activeTab === "liquidity" ? renderLiquidityPanel(symbol) : ""}
      `;
      detailEl.dataset.symbol = symbol.symbol;
      pendingDetailRefresh = false;
      if (activeTab === "ai") pendingAIRefreshSymbol = null;
      restoreDetailScroll(symbol.symbol, preservedScroll, activeTab);
    }

    function renderEvents(events) {
      alertCountEl.textContent = String(events.length);
      applyEventsCollapseState();
      if (!events.length) {
        eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
        return;
      }
      eventsEl.innerHTML = events.map((event) => {
        const reasons = (event.reasons || []).join("; ") || "暂无明确触发项";
        const suggestions = (event.suggestions || []).join("; ") || "继续观察盘口与量价变化";
        const aiSummary = (event.ai_summary || []).join("; ").trim();
        const directionClass = eventDirectionClass(event);
        const directionLabel = directionText[event.direction] || event.direction || "异常";
        return `
          <div class="event">
            <div class="event-head">
              <div class="event-main">
                <div class="event-title ${directionClass}">${esc(event.symbol)} <span class="event-score-text">${fmtNumber(event.score, 1)}/100</span></div>
                <div class="event-meta">
                  <span>${esc(event.created_at || "")}</span>
                  <span>${esc(eventLevel(event))}</span>
                  <span>${esc(event.risk_level || "")}</span>
                  <span>${esc(event.bias || "")}</span>
                </div>
              </div>
              <span class="event-badge ${directionClass}">${esc(directionLabel)}</span>
            </div>
            <div class="event-row"><span class="event-label">触发</span><span class="event-text">${esc(reasons)}</span></div>
            <div class="event-row"><span class="event-label">观察</span><span class="event-text muted">${esc(suggestions)}</span></div>
            ${aiSummary ? `<div class="event-row"><span class="event-label">AI</span><span class="event-text">${esc(aiSummary)}</span></div>` : ""}
          </div>
        `;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await apiFetch("/api/state", { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        const exchangeLabel = (data.exchange || "binance_usdm").startsWith("okx") ? "OKX" : "Binance";
        const transportLabel = data.data_source === "websocket" ? "WebSocket" : "REST";
        const symbols = data.symbols || [];
        sourceLabelEl.textContent = `${exchangeLabel} ${transportLabel}`;
        renderSymbols(symbols);
        if (shouldDeferDetailRender(symbols)) {
          pendingDetailRefresh = true;
        } else {
          pendingDetailRefresh = false;
          pendingAIRefreshSymbol = null;
          renderDetail(symbols);
        }
        renderEvents(data.events || []);
        const timeText = new Date().toLocaleTimeString();
        updatedEl.textContent = data.source_note ? `${data.source_note} · ${timeText}` : `已更新 ${timeText}`;
      } catch (error) {
        updatedEl.textContent = "面板连接中断";
      }
    }

    async function saveSymbols() {
      const symbols = symbolInputEl.value
        .split(/[\\s,，;；]+/)
        .map((symbol) => symbol.trim())
        .filter(Boolean);

      saveSymbolsEl.disabled = true;
      saveSymbolsEl.textContent = "保存中";
      try {
        const response = await apiFetch("/api/symbols", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbols })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        inputTouched = false;
        await refresh();
        updatedEl.textContent = "监控列表已更新";
      } catch (error) {
        updatedEl.textContent = error.message || "保存失败";
      } finally {
        saveSymbolsEl.disabled = false;
        saveSymbolsEl.textContent = "保存监控";
      }
    }

    symbolInputEl.addEventListener("input", () => { inputTouched = true; });
    symbolInputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") saveSymbols();
    });
    saveSymbolsEl.addEventListener("click", saveSymbols);

    const btnTelegram = document.getElementById("btn-telegram");
    const btnAI = document.getElementById("btn-ai");
    const tgToggle = document.getElementById("tg-toggle");
    const tgToggleLabel = document.getElementById("tg-toggle-label");
    const tgUsersEl = document.getElementById("tg-users");
    const tgAddUserBtn = document.getElementById("tg-add-user-btn");
    const tgSaveBtn = document.getElementById("tg-save-btn");
    const tgTestBtn = document.getElementById("tg-test-btn");
    let tgUsers = [];
    let tgEnabled = false;

    function setToggle(toggle, label, enabled) {
      toggle.classList.toggle("active", enabled);
      label.textContent = enabled ? "开启" : "关闭";
    }

    function renderTgUsers() {
      tgUsersEl.innerHTML = tgUsers.map((user, index) => `
        <div class="telegram-user" data-index="${index}">
          <input class="tg-user-name" placeholder="用户名" value="${esc(user.name || "")}">
          <input class="tg-user-token" type="password" placeholder="Bot Token" value="${user.bot_token_set ? "********" : esc(user.bot_token || "")}">
          <input class="tg-user-chat" placeholder="Chat ID，多个用逗号" value="${esc((user.chat_ids || []).join(", "))}">
          <button class="small-btn secondary tg-user-remove" type="button">删除</button>
        </div>
      `).join("");
      tgUsersEl.querySelectorAll(".tg-user-remove").forEach((button) => {
        button.addEventListener("click", () => {
          const row = button.closest(".telegram-user");
          tgUsers.splice(Number(row.dataset.index), 1);
          renderTgUsers();
        });
      });
    }

    async function loadTelegramConfig() {
      try {
        const response = await apiFetch("/api/telegram", { cache: "no-store" });
        const data = await response.json();
        tgEnabled = Boolean(data.enabled);
        tgUsers = data.users || [];
        if (!tgUsers.length) {
          tgUsers = [{ name: "默认用户", enabled: true, bot_token: "", chat_ids: [] }];
        }
        setToggle(tgToggle, tgToggleLabel, tgEnabled);
        renderTgUsers();
      } catch (error) {
        updatedEl.textContent = "推送配置读取失败";
      }
    }

    function collectTgUsers() {
      return Array.from(tgUsersEl.querySelectorAll(".telegram-user")).map((row, index) => {
        const chatIds = row.querySelector(".tg-user-chat").value
          .split(/[\\s,，;；]+/)
          .map((item) => item.trim())
          .filter(Boolean);
        return {
          name: row.querySelector(".tg-user-name").value.trim() || `用户${index + 1}`,
          enabled: true,
          bot_token: row.querySelector(".tg-user-token").value.trim(),
          chat_ids: chatIds
        };
      });
    }

    async function saveTelegramConfig() {
      const body = { enabled: tgEnabled, users: collectTgUsers() };
      try {
        const response = await apiFetch("/api/telegram", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        updatedEl.textContent = "推送设置已保存";
        closeModal(telegramModal);
      } catch (error) {
        updatedEl.textContent = error.message || "推送设置保存失败";
      }
    }

    async function testTelegramConfig() {
      const body = { enabled: tgEnabled, users: collectTgUsers() };
      tgTestBtn.disabled = true;
      tgTestBtn.textContent = "发送中";
      try {
        const response = await apiFetch("/api/telegram/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "测试失败");
        const sent = data.result && data.result.sent ? data.result.sent : 0;
        updatedEl.textContent = `测试推送已发送 ${sent} 条`;
      } catch (error) {
        updatedEl.textContent = error.message || "测试推送失败";
      } finally {
        tgTestBtn.disabled = false;
        tgTestBtn.textContent = "发送测试";
      }
    }

    function setOfStars(value) {
      return value.length > 0 && value.split("").every((char) => char === "*");
    }

    btnTelegram.addEventListener("click", async () => {
      await loadTelegramConfig();
      openModal(telegramModal);
    });
    tgToggle.addEventListener("click", () => {
      tgEnabled = !tgEnabled;
      setToggle(tgToggle, tgToggleLabel, tgEnabled);
    });
    tgAddUserBtn.addEventListener("click", () => {
      tgUsers.push({ name: `用户${tgUsers.length + 1}`, enabled: true, bot_token: "", chat_ids: [] });
      renderTgUsers();
    });
    tgSaveBtn.addEventListener("click", saveTelegramConfig);
    tgTestBtn.addEventListener("click", testTelegramConfig);

    const aiToggle = document.getElementById("ai-toggle");
    const aiToggleLabel = document.getElementById("ai-toggle-label");
    const aiProvider = document.getElementById("ai-provider");
    const aiBaseUrl = document.getElementById("ai-base-url");
    const aiKey = document.getElementById("ai-key");
    const aiModel = document.getElementById("ai-model");
    const aiThreshold = document.getElementById("ai-threshold");
    const aiRetryCooldown = document.getElementById("ai-retry-cooldown");
    const aiTimeout = document.getElementById("ai-timeout");
    const aiTriggerMode = document.getElementById("ai-trigger-mode");
    const aiTriggerFields = {
      score: [document.getElementById("ai-trigger-score"), document.getElementById("ai-trigger-score-value")],
      quote_volume_1m: [document.getElementById("ai-trigger-volume"), document.getElementById("ai-trigger-volume-value")],
      volume_multiplier: [document.getElementById("ai-trigger-multiplier"), document.getElementById("ai-trigger-multiplier-value")],
      price_move_pct_1m_abs: [document.getElementById("ai-trigger-price"), document.getElementById("ai-trigger-price-value")],
      oi_change_pct_5m_abs: [document.getElementById("ai-trigger-oi"), document.getElementById("ai-trigger-oi-value")],
      liquidation_total_quote_1m: [document.getElementById("ai-trigger-liquidation"), document.getElementById("ai-trigger-liquidation-value")]
    };
    const aiSaveBtn = document.getElementById("ai-save-btn");
    let aiEnabled = false;

    async function loadAIConfig() {
      try {
        const response = await apiFetch("/api/ai/config", { cache: "no-store" });
        const data = await response.json();
        aiEnabled = Boolean(data.enabled);
        setToggle(aiToggle, aiToggleLabel, aiEnabled);
        aiProvider.value = data.provider || "openai";
        aiBaseUrl.value = data.base_url || "";
        aiKey.value = data.api_key || "";
        aiModel.value = data.model || "gpt-4o-mini";
        aiThreshold.value = data.activation_threshold || 60;
        aiRetryCooldown.value = data.retry_cooldown_seconds || 120;
        aiTimeout.value = data.request_timeout_seconds || 30;
        const triggers = data.triggers || {};
        const conditions = triggers.conditions || {};
        aiTriggerMode.value = triggers.mode || "any";
        Object.entries(aiTriggerFields).forEach(([key, fields]) => {
          const cfg = conditions[key] || {};
          fields[0].checked = Boolean(cfg.enabled ?? (key === "score"));
          fields[1].value = cfg.threshold ?? fields[1].value;
        });
      } catch (error) {
        updatedEl.textContent = "AI 配置读取失败";
      }
    }

    function collectAITriggers() {
      const conditions = {};
      Object.entries(aiTriggerFields).forEach(([key, fields]) => {
        conditions[key] = {
          enabled: fields[0].checked,
          threshold: Number(fields[1].value) || 0
        };
      });
      return { mode: aiTriggerMode.value || "any", conditions };
    }

    async function saveAIConfig() {
      const body = {
        enabled: aiEnabled,
        provider: aiProvider.value,
        base_url: aiBaseUrl.value.trim(),
        model: aiModel.value.trim() || "gpt-4o-mini",
        activation_threshold: Number(aiThreshold.value) || 60,
        retry_cooldown_seconds: Number(aiRetryCooldown.value) || 120,
        request_timeout_seconds: Math.max(5, Math.min(30, Number(aiTimeout.value) || 30)),
        triggers: collectAITriggers()
      };
      if (aiKey.value && !aiKey.value.includes("***") && setOfStars(aiKey.value) === false) {
        body.api_key = aiKey.value;
      }
      try {
        const response = await apiFetch("/api/ai/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        aiResults = {};
        updatedEl.textContent = "AI 设置已保存";
        closeModal(aiModal);
      } catch (error) {
        updatedEl.textContent = error.message || "AI 设置保存失败";
      }
    }

    btnAI.addEventListener("click", async () => {
      await loadAIConfig();
      openModal(aiModal);
    });
    aiToggle.addEventListener("click", () => {
      aiEnabled = !aiEnabled;
      setToggle(aiToggle, aiToggleLabel, aiEnabled);
    });
    aiSaveBtn.addEventListener("click", saveAIConfig);

    async function loadSymbolThresholds() {
      try {
        const response = await apiFetch("/api/symbol_thresholds", { cache: "no-store" });
        const data = await response.json();
        globalThreshold = Number(data.default_score || 70);
        symbolThresholds = data.symbol_thresholds || {};
      } catch (error) {
        symbolThresholds = {};
      }
    }

    function openThresholdModal(symbol) {
      thresholdEditingSymbol = symbol;
      const threshold = symbolThresholds[symbol] || {};
      thresholdSymbolEl.value = symbol;
      thresholdInputEl.value = threshold && threshold.anomaly_score !== undefined ? threshold.anomaly_score : "";
      thresholdInputEl.placeholder = String(globalThreshold);
      applyThresholdRuleForm(threshold.push_rules, threshold.anomaly_score ?? globalThreshold);
      thresholdHintEl.textContent = `全局默认 ${fmtNumber(globalThreshold, 1)} 分；当前 ${currentThresholdText(symbol)}。`;
      openModal(thresholdModal);
      thresholdInputEl.focus();
    }

    async function saveSymbolThreshold(symbol, config) {
      try {
        const response = await apiFetch("/api/symbol_thresholds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol, anomaly_score: config.anomaly_score, push_rules: config.push_rules })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        if (config.anomaly_score === null && !hasEnabledThresholdRules(config.push_rules)) {
          delete symbolThresholds[symbol];
        } else {
          const nextConfig = {};
          if (config.anomaly_score !== null) nextConfig.anomaly_score = Number(config.anomaly_score);
          if (hasEnabledThresholdRules(config.push_rules)) nextConfig.push_rules = config.push_rules;
          symbolThresholds[symbol] = nextConfig;
        }
        updatedEl.textContent = `${symbol} 推送规则已更新`;
        closeModal(thresholdModal);
        await refresh();
      } catch (error) {
        updatedEl.textContent = error.message || "规则保存失败";
      }
    }

    document.getElementById("threshold-save-btn").addEventListener("click", () => {
      if (!thresholdEditingSymbol) return;
      const raw = thresholdInputEl.value.trim();
      const pushRules = collectThresholdRules();
      const parsedScore = Number(raw);
      const anomalyScore = raw === "" || Number.isNaN(parsedScore)
        ? null
        : Math.max(0, Math.min(100, parsedScore));
      if (raw === "") {
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: null, push_rules: pushRules });
      } else {
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: anomalyScore, push_rules: pushRules });
      }
    });
    document.getElementById("threshold-reset-btn").addEventListener("click", () => {
      if (thresholdEditingSymbol) {
        applyThresholdRuleForm(null, globalThreshold);
        thresholdInputEl.value = "";
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: null, push_rules: null });
      }
    });

    async function fetchAIAnalysis(symbol, force = false) {
      const aiBlock = document.getElementById("ai-block");
      if (aiBlock) {
        aiScrollBySymbol[symbol] = aiBlock.scrollTop;
        aiMeta[symbol] = { status: "分析中", ts: Date.now() };
        refreshAIBlock(symbol);
      }
      aiRequestedAt[symbol] = Date.now();
      try {
        const url = `/api/ai/analysis?symbol=${encodeURIComponent(symbol)}${force ? "&force=1" : ""}`;
        const response = await apiFetch(url, { cache: "no-store" });
        const data = await response.json();
        if (data.analysis) {
          saveAIResult(symbol, data.analysis);
          aiMeta[symbol] = {
            status: data.cached ? "使用缓存" : "已更新",
            ts: Date.now()
          };
        } else {
          const reason = data.reason || "暂无分析";
          if (reason === "ai trigger not met") {
            aiMeta[symbol] = { status: "触发条件未满足", ts: Date.now() };
            if (aiBlock && selectedSymbol === symbol) refreshAIBlock(symbol);
            updatedEl.textContent = "AI 触发条件未满足";
            return;
          }
          if (reason === "retry cooldown") {
            aiMeta[symbol] = { status: "冷却中", ts: Date.now() };
          } else {
            aiMeta[symbol] = { status: reason, ts: Date.now() };
            aiResults[symbol] = reason;
          }
        }
        if (aiBlock && selectedSymbol === symbol) refreshAIBlock(symbol);
        updatedEl.textContent = data.cached ? "AI 分析使用缓存" : "AI 分析已更新";
      } catch (error) {
        if (aiBlock) {
          aiMeta[symbol] = { status: "AI 请求失败", ts: Date.now() };
          refreshAIBlock(symbol);
        }
        updatedEl.textContent = "AI 请求失败";
      }
    }

    function maybeLoadAIAnalysis(symbol) {
      if (!symbol || aiResults[symbol.symbol]) return;
      const lastRequested = aiRequestedAt[symbol.symbol] || 0;
      if (Date.now() - lastRequested < 60000) return;
      fetchAIAnalysis(symbol.symbol, false);
    }

    detailEl.addEventListener("click", (event) => {
      const tabButton = event.target.closest("[data-detail-tab]");
      if (tabButton) {
        captureDetailScroll(selectedSymbol);
        setDetailTab(tabButton.dataset.detailTab);
        renderDetail(lastSymbols);
        return;
      }
      if (event.target.id === "ai-refresh-btn" && selectedSymbol) {
        fetchAIAnalysis(selectedSymbol, true);
      }
    });
    detailEl.addEventListener("scroll", (event) => {
      if (event.target && event.target.id === "ai-block" && selectedSymbol) {
        aiScrollBySymbol[selectedSymbol] = event.target.scrollTop;
        noteAIInteraction();
      }
    }, true);
    detailEl.addEventListener("wheel", (event) => {
      if (event.target && event.target.closest && event.target.closest("#ai-block") && selectedSymbol) {
        noteAIInteraction();
      }
    }, { passive: true });
    detailEl.addEventListener("touchmove", (event) => {
      if (event.target && event.target.closest && event.target.closest("#ai-block") && selectedSymbol) {
        noteAIInteraction();
      }
    }, { passive: true });
    if (sideScrollEl) {
      sideScrollEl.addEventListener("scroll", () => {
        if (selectedSymbol) drawerScrollBySymbol[selectedSymbol] = sideScrollEl.scrollTop;
      });
    }
    detailCloseBtn.addEventListener("click", () => closeDetailDrawer(true));
    detailDrawerBackdropEl.addEventListener("click", () => closeDetailDrawer(true));

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-collapse]");
      if (!button) return;
      const section = button.dataset.collapse;
      if (!section) return;
      const collapsed = !isCollapsed(section);
      setCollapsed(section, collapsed);
      updateCollapseButton(button, collapsed);
      const block = button.closest(".collapsible");
      if (block) block.classList.toggle("collapsed", collapsed);
      if (section === "events") applyEventsCollapseState();
    });

    document.getElementById("profile-open-telegram").addEventListener("click", async () => {
      closeModal(profileModal);
      await loadTelegramConfig();
      openModal(telegramModal);
    });
    document.getElementById("profile-open-ai").addEventListener("click", async () => {
      closeModal(profileModal);
      await loadAIConfig();
      openModal(aiModal);
    });
    document.getElementById("profile-logout").addEventListener("click", () => {
      closeModal(profileModal);
      logout();
    });

    [
      ["close-profile-modal", profileModal],
      ["close-profile-btn", profileModal],
      ["close-telegram-modal", telegramModal],
      ["close-telegram-btn", telegramModal],
      ["close-ai-modal", aiModal],
      ["close-ai-btn", aiModal],
      ["close-threshold-modal", thresholdModal],
      ["close-threshold-btn", thresholdModal]
    ].forEach(([id, modal]) => {
      document.getElementById(id).addEventListener("click", () => closeModal(modal));
    });
    [profileModal, telegramModal, aiModal, thresholdModal].forEach((modal) => {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeModal(modal);
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeModal(profileModal);
        closeModal(telegramModal);
        closeModal(aiModal);
        closeModal(thresholdModal);
        closeDetailDrawer(true);
      }
    });

    bootstrap();
  </script>
</body>
</html>
"""
