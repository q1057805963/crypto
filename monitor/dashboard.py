import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time

from monitor.ai_analysis import AIAnalyzer
from monitor.auth import AuthError, AuthManager
from monitor.dashboard_assets import read_dashboard_html, read_static_asset
from monitor.dashboard_state import DashboardState, normalize_symbols
from monitor.rules import enabled_trigger_count
from monitor.telegram import normalize_telegram_users, send_text_to_telegram_users
from monitor.timeframe_analysis import TIMEFRAME_CONFIG, TimeframeAnalysisService
from monitor.user_config import UserConfigStore


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
        period_liquidation_provider=None,
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
        self.period_liquidation_provider = period_liquidation_provider
        self._ai_analyzers: dict[str, AIAnalyzer] = {}
        self._event_loop = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._config_lock = threading.Lock()
        self._auth_attempt_lock = threading.Lock()
        self._auth_attempts: dict[str, list[float]] = {}
        self.timeframe_analysis = TimeframeAnalysisService()

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

                if path.startswith("/static/"):
                    asset = read_static_asset(path.removeprefix("/static/"))
                    if not asset:
                        self.send_error(404)
                        return
                    body, content_type = asset
                    self._send_bytes(body, content_type)
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

                if path == "/api/timeframe":
                    self._handle_timeframe_analysis_get()
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
                period = params.get("period", ["5m"])[0]
                force_refresh = (params.get("force", ["0"])[0] == "1")
                if not symbol:
                    self._send_json({"analysis": None, "reason": "symbol required"})
                    return
                if period != "realtime" and period not in TIMEFRAME_CONFIG:
                    self._send_json({"analysis": None, "reason": "unsupported period"}, status=400)
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
                timeframe_data = None
                if period != "realtime":
                    try:
                        source = state.get_source()
                        timeframe_data = server_ref.timeframe_analysis.analyze(
                            symbol=symbol,
                            period=period,
                            exchange=str(source.get("exchange", "binance_usdm")),
                            force=force_refresh,
                        )
                        if server_ref.period_liquidation_provider:
                            timeframe_data.update(
                                server_ref.period_liquidation_provider(
                                    symbol,
                                    int(TIMEFRAME_CONFIG[period]["seconds"]),
                                )
                            )
                    except Exception:
                        timeframe_data = None
                cached = analyzer.get_cached(symbol, period=period)
                if cached and not force_refresh:
                    self._send_json({"analysis": cached, "cached": True})
                    return
                loop = server_ref._event_loop
                if loop:
                    import asyncio
                    future = asyncio.run_coroutine_threadsafe(
                        analyzer.analyze(
                            symbol,
                            snapshot_data,
                            timeframe_data=timeframe_data,
                            period=period,
                            force=force_refresh,
                        ),
                        loop,
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

            def _handle_timeframe_analysis_get(self) -> None:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                symbol = (params.get("symbol", [""])[0]).upper()
                period = params.get("period", ["5m"])[0]
                force = params.get("force", ["0"])[0] == "1"
                if not symbol:
                    self._send_json({"ok": False, "error": "symbol required"}, status=400)
                    return
                if period not in TIMEFRAME_CONFIG:
                    self._send_json({"ok": False, "error": "unsupported period"}, status=400)
                    return
                try:
                    source = state.get_source()
                    analysis = server_ref.timeframe_analysis.analyze(
                        symbol=symbol,
                        period=period,
                        exchange=str(source.get("exchange", "binance_usdm")),
                        force=force,
                    )
                    if server_ref.period_liquidation_provider:
                        analysis.update(
                            server_ref.period_liquidation_provider(
                                symbol,
                                int(TIMEFRAME_CONFIG[period]["seconds"]),
                            )
                        )
                    self._send_json({"ok": True, "analysis": analysis})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=502)

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
                self._send_bytes(body, content_type)

            def _send_bytes(self, body: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


INDEX_HTML = read_dashboard_html()
