import asyncio
import json
import logging
import re
from time import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from monitor.telegram import send_telegram_chat_action, send_telegram_message


class TelegramBotResponder:
    def __init__(
        self,
        enabled: bool,
        get_users,
        get_snapshot,
        get_ai_analyzer,
        get_timeframe_context=None,
        poll_interval_seconds: float = 2,
        request_timeout_seconds: int = 20,
        ai_cooldown_seconds: int = 20,
        history_ttl_seconds: int = 1800,
        history_max_rounds: int = 3,
        timeframe_context_timeout_seconds: float = 15,
    ) -> None:
        self.enabled = bool(enabled)
        self.get_users = get_users
        self.get_snapshot = get_snapshot
        self.get_ai_analyzer = get_ai_analyzer
        self.get_timeframe_context = get_timeframe_context
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.ai_cooldown_seconds = ai_cooldown_seconds
        self.history_ttl_seconds = int(history_ttl_seconds)
        self.history_max_rounds = int(history_max_rounds)
        self.timeframe_context_timeout_seconds = float(timeframe_context_timeout_seconds)
        self._offsets: dict[str, int] = {}
        self._initialized_tokens: set[str] = set()
        self._last_question_at: dict[str, float] = {}
        self._history: dict[str, list[dict]] = {}
        self._last_symbol: dict[str, tuple[str, float]] = {}
        self._started_at = time()

    async def run(self) -> None:
        if not self.enabled:
            return

        logging.info("Telegram bot question responder enabled")
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("Telegram bot responder failed: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self) -> None:
        token_groups = self._token_groups()
        for bot_token, users in token_groups.items():
            offset = self._offsets.get(bot_token)
            updates = await asyncio.to_thread(self._get_updates, bot_token, offset)
            if not updates:
                self._initialized_tokens.add(bot_token)
                continue

            next_offset = max(int(update.get("update_id", 0)) for update in updates) + 1
            self._offsets[bot_token] = next_offset

            if bot_token not in self._initialized_tokens:
                self._initialized_tokens.add(bot_token)
                updates = [
                    update
                    for update in updates
                    if float((update.get("message") or {}).get("date") or 0) >= self._started_at - 2
                ]
                if not updates:
                    continue

            for update in updates:
                await self._handle_update(bot_token, users, update)

    def _token_groups(self) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for user in self.get_users() or []:
            if not user.get("enabled", True):
                continue
            bot_token = str(user.get("bot_token") or "").strip()
            chat_ids = [str(chat_id).strip() for chat_id in user.get("chat_ids", [])]
            if not bot_token or not chat_ids:
                continue
            groups.setdefault(bot_token, []).append(user)
        return groups

    def _get_updates(self, bot_token: str, offset: int | None) -> list[dict]:
        params = {
            "timeout": "10",
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = str(offset)
        request = Request(
            f"https://api.telegram.org/bot{bot_token}/getUpdates?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise ValueError(payload.get("description") or "getUpdates failed")
        return payload.get("result") or []

    async def _handle_update(self, bot_token: str, users: list[dict], update: dict) -> None:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if not text or not chat_id:
            return

        user = self._match_user(users, chat_id)
        if not user:
            return

        if self._is_help(text):
            self._send(bot_token, chat_id, self._help_text(user))
            return

        session_key = f"{bot_token}:{chat_id}"
        symbol = self._extract_symbol(text, user.get("symbols") or [])
        if not symbol:
            symbol = self._recent_symbol(session_key)
        if not symbol:
            self._send(bot_token, chat_id, self._help_text(user))
            return

        now = time()
        last_question_at = self._last_question_at.get(session_key, 0)
        if now - last_question_at < self.ai_cooldown_seconds:
            wait_seconds = int(self.ai_cooldown_seconds - (now - last_question_at))
            self._send(bot_token, chat_id, f"AI 分析冷却中，请约 {wait_seconds} 秒后再问。")
            return
        self._last_question_at[session_key] = now

        snapshot = self.get_snapshot(symbol)
        if not snapshot:
            self._send(bot_token, chat_id, f"{symbol} 暂无实时快照，等监控采集到数据后再问。")
            return

        analyzer = self.get_ai_analyzer(str(user.get("owner_id") or ""))
        if not analyzer or not analyzer.enabled:
            self._send(bot_token, chat_id, "AI 未开启，请先在页面的 AI 设置里启用并配置模型。")
            return
        if not analyzer.api_key:
            self._send(bot_token, chat_id, "AI API Key 未配置，请先在页面的 AI 设置里填写。")
            return

        self._last_symbol[session_key] = (symbol, now)
        history = self._recent_history(session_key)
        available_symbols = [str(item).upper() for item in user.get("symbols", [])]

        async def answer_worker() -> str | None:
            timeframe_data, confluence_data = await self._load_timeframe_context(symbol, text)
            return await analyzer.answer_question(
                text,
                snapshot,
                available_symbols,
                history=history,
                timeframe_data=timeframe_data,
                confluence_data=confluence_data,
            )

        answer_task = asyncio.create_task(answer_worker())
        result = await self._wait_with_typing(bot_token, chat_id, answer_task)
        if not result:
            reason = analyzer.get_last_error() if analyzer else "analysis failed"
            self._send(bot_token, chat_id, f"AI 暂时没有返回结果：{reason}")
            return

        reply = self._sanitize_reply(result)
        self._remember(session_key, question=text, answer=reply, symbol=symbol)
        self._send(bot_token, chat_id, reply)

    async def _wait_with_typing(
        self,
        bot_token: str,
        chat_id: str,
        task: asyncio.Task,
        max_wait_seconds: float = 90,
    ) -> str | None:
        deadline = time() + max_wait_seconds
        try:
            while True:
                await asyncio.to_thread(self._send_chat_action, bot_token, chat_id)
                remaining = deadline - time()
                if remaining <= 0:
                    task.cancel()
                    return None
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=min(4.5, remaining))
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _load_timeframe_context(self, symbol: str, text: str) -> tuple[dict | None, dict | None]:
        if not self.get_timeframe_context:
            return None, None
        period = self._detect_period(text)
        try:
            context = await asyncio.wait_for(
                asyncio.to_thread(self.get_timeframe_context, symbol, period),
                timeout=self.timeframe_context_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.debug("Telegram bot timeframe context failed for %s: %s", symbol, exc)
            return None, None
        if not context:
            return None, None
        timeframe_data, confluence_data = context
        return timeframe_data, confluence_data

    @staticmethod
    def _detect_period(text: str) -> str | None:
        lowered = str(text or "").lower()
        period_rules = [
            ("1d", ("1d", "日线", "日k", "天图", "1天", "一天", "daily")),
            ("4h", ("4h", "4小时", "四小时")),
            ("1h", ("1h", "1小时", "一小时", "60分")),
            ("15m", ("15m", "15分", "十五分")),
            ("5m", ("5m", "5分", "五分")),
        ]
        for period, keywords in period_rules:
            if any(keyword in lowered for keyword in keywords):
                return period
        return None

    def _recent_history(self, session_key: str) -> list[dict]:
        now = time()
        entries = [
            entry
            for entry in self._history.get(session_key, [])
            if now - float(entry.get("at") or 0) <= self.history_ttl_seconds
        ]
        if entries:
            self._history[session_key] = entries
        else:
            self._history.pop(session_key, None)
        return list(entries)

    def _remember(self, session_key: str, *, question: str, answer: str, symbol: str) -> None:
        entries = self._recent_history(session_key)
        entries.append(
            {
                "question": str(question),
                "answer": str(answer),
                "symbol": str(symbol).upper(),
                "at": time(),
            }
        )
        self._history[session_key] = entries[-self.history_max_rounds:]

    def _recent_symbol(self, session_key: str) -> str | None:
        record = self._last_symbol.get(session_key)
        if not record:
            return None
        symbol, at = record
        if time() - at > self.history_ttl_seconds:
            self._last_symbol.pop(session_key, None)
            return None
        return symbol

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"```[a-zA-Z0-9_-]*", "", cleaned)
        cleaned = cleaned.replace("**", "").replace("__", "")
        cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"^(\s*)[*•]\s+", r"\1- ", cleaned, flags=re.MULTILINE)
        return cleaned.strip()

    @staticmethod
    def _send_chat_action(bot_token: str, chat_id: str) -> None:
        error = send_telegram_chat_action(bot_token, chat_id)
        if error:
            logging.debug("Telegram chat action to %s failed: %s", chat_id, error)

    @staticmethod
    def _match_user(users: list[dict], chat_id: str) -> dict | None:
        for user in users:
            chat_ids = {str(value).strip() for value in user.get("chat_ids", [])}
            if chat_id in chat_ids:
                return user
        return None

    @staticmethod
    def _is_help(text: str) -> bool:
        command = text.strip().split()[0].lower()
        return command in {"/start", "/help", "帮助", "help"}

    @staticmethod
    def _extract_symbol(text: str, allowed_symbols: list[str]) -> str | None:
        allowed = [str(symbol).upper() for symbol in allowed_symbols if str(symbol).strip()]
        normalized_text = re.sub(r"[^A-Z0-9]", "", text.upper())
        for symbol in sorted(allowed, key=len, reverse=True):
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            if symbol in normalized_text or base in normalized_text:
                return symbol

        ignored = {
            "ASK", "START", "HELP", "USDT", "LONG", "SHORT",
            "OI", "VWAP", "POC", "AI", "ATH", "ATL", "KDJ", "MACD", "RSI",
        }
        for token in re.findall(r"[A-Za-z0-9]{2,20}", text.upper()):
            if token in ignored or token.isdigit():
                continue
            symbol = token if token.endswith("USDT") else f"{token}USDT"
            if not allowed or symbol in allowed:
                return symbol

        if len(allowed) == 1:
            return allowed[0]
        return None

    @staticmethod
    def _help_text(user: dict) -> str:
        symbols = ", ".join((user.get("symbols") or [])[:12]) or "暂无"
        return (
            "可以直接问我合约问题，例如：\n"
            "- BTC 现在能追吗？\n"
            "- /ask ETH 会不会急跌？\n"
            "- SOL 当前风险点是什么？\n\n"
            f"当前可查询：{symbols}"
        )

    @staticmethod
    def _send(bot_token: str, chat_id: str, text: str) -> None:
        chunks = []
        remaining = str(text)
        while len(remaining) > 3500:
            chunks.append(remaining[:3500])
            remaining = remaining[3500:]
        chunks.append(remaining)

        for chunk in chunks:
            error = send_telegram_message(bot_token, chat_id, chunk)
            if error:
                logging.warning("Telegram bot reply to %s failed: %s", chat_id, error)
