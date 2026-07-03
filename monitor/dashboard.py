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
from monitor.telegram import normalize_telegram_users
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
    def __init__(self, symbols: list[str], data_source: str) -> None:
        self._lock = threading.Lock()
        self._data_source = data_source
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
                "long_liquidation_quote_1m": 0,
                "short_liquidation_quote_1m": 0,
                "liquidation_total_quote_1m": 0,
                "liquidation_event_count_1m": 0,
                "liquidation_data_status": "unavailable",
                "microstructure_status": "unavailable",
                "depth_data_age_seconds": None,
                "last_liquidation_age_seconds": None,
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
                        "long_liquidation_quote_1m": 0,
                        "short_liquidation_quote_1m": 0,
                        "liquidation_total_quote_1m": 0,
                        "liquidation_event_count_1m": 0,
                        "liquidation_data_status": "unavailable",
                        "microstructure_status": "unavailable",
                        "depth_data_age_seconds": None,
                        "last_liquidation_age_seconds": None,
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
            data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._events.insert(0, data)
            self._events = self._events[:50]

    def get_symbol_data(self, symbol: str) -> dict | None:
        with self._lock:
            return self._symbols.get(symbol.upper())

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

            def _handle_symbol_thresholds_post(self) -> None:
                try:
                    payload = self._read_json()
                    symbol = str(payload.get("symbol", "")).upper()
                    if not symbol:
                        raise ValueError("symbol is required")
                    score = payload.get("anomaly_score")
                    with server_ref._config_lock:
                        if server_ref.user_config_store:
                            user_id = self._user_id()
                            st = dict(server_ref._get_user_config(user_id).get("symbol_thresholds", {}))
                        else:
                            user_id = ""
                            st = dict(server_ref.config.get("symbol_thresholds", {}))
                        if score is None:
                            st.pop(symbol, None)
                            if server_ref.detector and not server_ref.user_config_store:
                                server_ref.detector.remove_symbol_threshold(symbol)
                        else:
                            st[symbol] = {"anomaly_score": float(score)}
                            if server_ref.detector and not server_ref.user_config_store:
                                server_ref.detector.set_symbol_threshold(symbol, float(score))
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
      border: 1px solid #344252;
      border-radius: 999px;
      background: var(--blue-soft);
      color: var(--blue);
      padding: 0 9px;
      font-size: 11px;
      cursor: pointer;
      flex: 0 0 auto;
    }

    .row-action-btn:hover {
      border-color: var(--blue);
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

    .inline-link {
      border: none;
      background: none;
      color: var(--blue);
      cursor: pointer;
      font-size: 12px;
      padding: 0;
    }

    .inline-link:hover {
      text-decoration: underline;
    }

    .ai-inline {
      margin-top: 8px;
      padding: 10px 12px;
      background: rgba(100, 168, 255, .06);
      border: 1px solid rgba(100, 168, 255, .15);
      border-radius: 6px;
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
      display: grid;
      grid-template-columns: minmax(0, 1fr) 400px;
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

    .events {
      display: flex;
      flex-direction: column;
      height: 100%;
    }

    .side-scroll {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
    }

    .detail {
      padding: 16px;
      border-bottom: 1px solid var(--line);
    }

    .detail-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
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
      border-radius: 6px;
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

    .detail-block {
      margin-top: 12px;
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
      .events {
        height: auto;
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
        <div class="modal-title">推送阈值设置</div>
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
          <div class="setting-help" id="threshold-hint">留空或恢复默认时，将回退到全局阈值。</div>
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

  <main>
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

    <section class="events">
      <div class="section-title">
        <span>合约详情</span>
        <span id="source-label">REST</span>
      </div>
      <div class="side-scroll" id="side-scroll">
        <div class="detail" id="detail"></div>
        <div class="section-title">
          <span>最近报警</span>
          <span id="alert-count">0</span>
        </div>
        <div class="event-list" id="events"></div>
      </div>
    </section>
  </main>

  <script>
    const symbolsEl = document.getElementById("symbols");
    const eventsEl = document.getElementById("events");
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
      symbolInputEl.value = "";
      symbolsEl.innerHTML = "";
      eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
      detailEl.innerHTML = `<div class="empty">等待行情数据</div>`;
      countEl.textContent = "0 个合约";
      alertCountEl.textContent = "0";
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

    function liquidationStatusText(symbol) {
      const status = symbol.liquidation_data_status || "unavailable";
      if (status === "recent_event") return "有强平";
      if (status === "no_recent_event") return "0 / 无强平";
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
        return `<span class="muted">0 / 无强平</span>`;
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
      const threshold = symbolThresholds[symbol];
      if (threshold && threshold.anomaly_score !== undefined && threshold.anomaly_score !== null) {
        return `${fmtNumber(threshold.anomaly_score, 1)} 分`;
      }
      return `全局 ${fmtNumber(globalThreshold, 1)} 分`;
    }

    function aiStatusLine(symbol) {
      const meta = aiMeta[symbol];
      if (!meta) return "";
      const timeText = meta.ts ? new Date(meta.ts).toLocaleTimeString() : "";
      return `<div class="ai-status"><strong>${esc(meta.status)}</strong><span>${esc(timeText)}</span></div>`;
    }

    function renderAIBlock(symbol) {
      const text = aiResults[symbol];
      if (!text) {
        return `${aiStatusLine(symbol)}<div class="muted">等待 AI 根据当前合约指标生成观察建议。</div>`;
      }
      return aiStatusLine(symbol) + text.split("\\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => `<div>${esc(line)}</div>`)
        .join("");
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

    function renderSymbols(symbols) {
      lastSymbols = symbols || [];
      countEl.textContent = `${symbols.length} 个合约`;
      if (!selectedSymbol && symbols.length) selectedSymbol = symbols[0].symbol;
      if (selectedSymbol && !symbols.some((symbol) => symbol.symbol === selectedSymbol)) {
        selectedSymbol = symbols[0] ? symbols[0].symbol : null;
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
              <button class="row-action-btn js-threshold" data-symbol="${esc(symbol.symbol)}" type="button" title="设置推送阈值">阈值</button>
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
          renderSymbols(symbols);
          renderDetail(symbols);
        });
      });
      symbolsEl.querySelectorAll(".js-threshold").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          selectedSymbol = button.dataset.symbol;
          renderSymbols(symbols);
          renderDetail(symbols);
          openThresholdModal(selectedSymbol);
        });
      });
    }

    function renderDetail(symbols) {
      const symbol = symbols.find((item) => item.symbol === selectedSymbol) || symbols[0];
      if (!symbol) {
        detailEl.innerHTML = `<div class="empty">等待行情数据</div>`;
        return;
      }

      selectedSymbol = symbol.symbol;
      const reasons = (symbol.reasons || []).length ? symbol.reasons : ["暂无明确触发项"];
      maybeLoadAIAnalysis(symbol);
      detailEl.innerHTML = `
        <div class="detail-head">
          <div class="detail-meta">
            <div class="detail-symbol">${esc(symbol.symbol)}</div>
            <div class="detail-price-wrap">
              <div class="detail-price-label">当前价格</div>
              <div class="detail-price ${valueClass(symbol.price_move_pct_1m)}">${fmtNumber(symbol.price, 8)}</div>
            </div>
            <div class="detail-bias">${esc(symbol.bias || "观察：暂无明确方向")}</div>
          </div>
          <span class="score">${fmtNumber(symbol.score, 1)}</span>
        </div>
        <div class="metric-grid">
          <div class="metric"><div class="metric-label">风险</div><div class="metric-value ${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</div></div>
          <div class="metric"><div class="metric-label">置信度</div><div class="metric-value">${fmtNumber(symbol.confidence, 1)}%</div></div>
          <div class="metric"><div class="metric-label">1分钟成交额</div><div class="metric-value">${fmtNumber(symbol.quote_volume_1m, 0)}</div></div>
          <div class="metric"><div class="metric-label">量能倍数</div><div class="metric-value ${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</div></div>
          <div class="metric"><div class="metric-label">OI 5分钟</div><div class="metric-value">${fmtPct(symbol.oi_change_pct_5m)}</div></div>
          <div class="metric"><div class="metric-label">资金费率</div><div class="metric-value">${fmtFunding(symbol.funding_rate)}</div></div>
          <div class="metric"><div class="metric-label">爆仓状态</div><div class="metric-value ${liquidationStatusClass(symbol)}">${liquidationStatusText(symbol)}</div></div>
          <div class="metric"><div class="metric-label">强平事件 1m</div><div class="metric-value">${fmtNumber(symbol.liquidation_event_count_1m, 0)}</div></div>
          <div class="metric"><div class="metric-label">多头爆仓 1m</div><div class="metric-value">${liquidationSideHtml(symbol, "long_liquidation_quote_1m")}</div></div>
          <div class="metric"><div class="metric-label">空头爆仓 1m</div><div class="metric-value">${liquidationSideHtml(symbol, "short_liquidation_quote_1m")}</div></div>
          <div class="metric"><div class="metric-label">微观结构</div><div class="metric-value">${microstructureStatusHtml(symbol)}</div></div>
          <div class="metric"><div class="metric-label">盘口点差</div><div class="metric-value">${fmtBps(symbol.spread_bps)} bps</div></div>
          <div class="metric"><div class="metric-label">盘口深度下降</div><div class="metric-value">${fmtNumber(symbol.depth_drop_pct_1m, 1)}%</div></div>
          <div class="metric"><div class="metric-label">买盘深度</div><div class="metric-value">${fmtNumber(symbol.bid_depth_notional, 0)}</div></div>
          <div class="metric"><div class="metric-label">卖盘深度</div><div class="metric-value">${fmtNumber(symbol.ask_depth_notional, 0)}</div></div>
          <div class="metric"><div class="metric-label">盘口失衡</div><div class="metric-value">${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%</div></div>
          <div class="metric"><div class="metric-label">主动买入</div><div class="metric-value">${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%</div></div>
        </div>
        <div class="detail-block">
          <div class="detail-title">触发原因</div>
          <div class="detail-list">${reasons.map((item) => `<div>${esc(item)}</div>`).join("")}</div>
        </div>
        <div class="detail-block">
          <div class="detail-title-row">
            <div class="detail-title">观察建议</div>
            <div class="detail-tools">
              <button class="inline-link" id="ai-refresh-btn" type="button">刷新</button>
            </div>
          </div>
          <div class="detail-list ai-inline" id="ai-block">${renderAIBlock(symbol.symbol)}</div>
        </div>
      `;
    }

    function renderEvents(events) {
      alertCountEl.textContent = String(events.length);
      if (!events.length) {
        eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
        return;
      }
      eventsEl.innerHTML = events.map((event) => {
        const reasons = (event.reasons || []).join("; ") || "暂无明确触发项";
        const suggestions = (event.suggestions || []).join("; ") || "继续观察盘口与量价变化";
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
          </div>
        `;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await apiFetch("/api/state", { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        sourceLabelEl.textContent = data.data_source === "websocket" ? "WebSocket" : "REST";
        renderSymbols(data.symbols || []);
        renderDetail(data.symbols || []);
        renderEvents(data.events || []);
        updatedEl.textContent = `已更新 ${new Date().toLocaleTimeString()}`;
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
      const threshold = symbolThresholds[symbol];
      thresholdSymbolEl.value = symbol;
      thresholdInputEl.value = threshold && threshold.anomaly_score !== undefined ? threshold.anomaly_score : "";
      thresholdInputEl.placeholder = String(globalThreshold);
      thresholdHintEl.textContent = `全局默认 ${fmtNumber(globalThreshold, 1)} 分，当前 ${currentThresholdText(symbol)}。`;
      openModal(thresholdModal);
      thresholdInputEl.focus();
    }

    async function saveSymbolThreshold(symbol, score) {
      try {
        const response = await apiFetch("/api/symbol_thresholds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol, anomaly_score: score })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        if (score === null) delete symbolThresholds[symbol];
        else symbolThresholds[symbol] = { anomaly_score: Number(score) };
        updatedEl.textContent = `${symbol} 推送阈值已更新`;
        closeModal(thresholdModal);
        await refresh();
      } catch (error) {
        updatedEl.textContent = error.message || "阈值保存失败";
      }
    }

    document.getElementById("threshold-save-btn").addEventListener("click", () => {
      if (!thresholdEditingSymbol) return;
      const raw = thresholdInputEl.value.trim();
      if (raw === "") {
        saveSymbolThreshold(thresholdEditingSymbol, null);
        return;
      }
      const score = Math.max(0, Math.min(100, Number(raw)));
      saveSymbolThreshold(thresholdEditingSymbol, score);
    });
    document.getElementById("threshold-reset-btn").addEventListener("click", () => {
      if (thresholdEditingSymbol) saveSymbolThreshold(thresholdEditingSymbol, null);
    });

    async function fetchAIAnalysis(symbol, force = false) {
      const aiBlock = document.getElementById("ai-block");
      if (aiBlock) aiBlock.innerHTML = `<div class="muted">分析中...</div>`;
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
            if (aiBlock && selectedSymbol === symbol) {
              aiBlock.innerHTML = renderAIBlock(symbol);
            }
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
        if (aiBlock && selectedSymbol === symbol) {
          aiBlock.innerHTML = renderAIBlock(symbol);
        }
        updatedEl.textContent = data.cached ? "AI 分析使用缓存" : "AI 分析已更新";
      } catch (error) {
        if (aiBlock) aiBlock.innerHTML = `<div class="muted">AI 请求失败</div>`;
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
      if (event.target.id === "ai-refresh-btn" && selectedSymbol) {
        fetchAIAnalysis(selectedSymbol, true);
      }
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
      }
    });

    bootstrap();
  </script>
</body>
</html>
"""
