import json
import logging
from urllib.request import Request, urlopen

from monitor.anomaly import AnomalyEvent


def normalize_telegram_users(
    users: list[dict] | None,
    legacy_bot_token: str = "",
    legacy_chat_ids: list[str] | None = None,
) -> list[dict]:
    normalized = []
    for index, user in enumerate(users or [], start=1):
        chat_ids = [
            str(chat_id).strip()
            for chat_id in user.get("chat_ids", [])
            if str(chat_id).strip()
        ]
        normalized.append(
            {
                "name": str(user.get("name") or f"用户{index}").strip(),
                "enabled": bool(user.get("enabled", True)),
                "bot_token": str(user.get("bot_token", "")),
                "chat_ids": chat_ids,
                "owner_id": str(user.get("owner_id", "")),
                "symbols": [str(symbol).upper() for symbol in user.get("symbols", [])],
                "symbol_thresholds": user.get("symbol_thresholds", {}),
                "default_score": float(user.get("default_score", 70)),
            }
        )

    if not normalized and (legacy_bot_token or legacy_chat_ids):
        normalized.append(
            {
                "name": "默认用户",
                "enabled": True,
                "bot_token": legacy_bot_token,
                "chat_ids": [
                    str(chat_id).strip()
                    for chat_id in (legacy_chat_ids or [])
                    if str(chat_id).strip()
                ],
                "owner_id": "",
                "symbols": [],
                "symbol_thresholds": {},
                "default_score": 70.0,
            }
        )
    return normalized


class TelegramAlert:
    def __init__(
        self,
        enabled: bool,
        bot_token: str = "",
        chat_ids: list[str] | None = None,
        users: list[dict] | None = None,
    ) -> None:
        self.requested_enabled = bool(enabled)
        self.users = normalize_telegram_users(users, bot_token, chat_ids or [])
        self.enabled = self.requested_enabled and any(self._user_ready(user) for user in self.users)

    def send(self, event: AnomalyEvent) -> None:
        if not self.enabled:
            return
        text = self._format(event)
        for user in self.users:
            if not self._user_ready(user):
                continue
            if not self._user_wants_event(user, event):
                continue
            for chat_id in user["chat_ids"]:
                self._send_to(user["bot_token"], chat_id, text)

    def set_chat_ids(self, chat_ids: list[str]) -> None:
        if not self.users:
            self.users = normalize_telegram_users(None, "", chat_ids)
        else:
            self.users[0]["chat_ids"] = [cid.strip() for cid in chat_ids if cid.strip()]
        self.enabled = self.requested_enabled and any(self._user_ready(user) for user in self.users)

    def set_config(
        self,
        enabled: bool,
        bot_token: str = "",
        chat_ids: list[str] | None = None,
        users: list[dict] | None = None,
    ) -> None:
        self.requested_enabled = bool(enabled)
        self.users = normalize_telegram_users(users, bot_token, chat_ids or [])
        self.enabled = self.requested_enabled and any(self._user_ready(user) for user in self.users)

    @staticmethod
    def _user_ready(user: dict) -> bool:
        return bool(user.get("enabled", True)) and bool(user.get("bot_token")) and bool(user.get("chat_ids"))

    @staticmethod
    def _user_wants_event(user: dict, event: AnomalyEvent) -> bool:
        symbols = {str(symbol).upper() for symbol in user.get("symbols", [])}
        if symbols and event.symbol.upper() not in symbols:
            return False
        symbol_thresholds = user.get("symbol_thresholds") or {}
        threshold = (
            symbol_thresholds.get(event.symbol.upper(), {}).get("anomaly_score")
            if isinstance(symbol_thresholds, dict)
            else None
        )
        if threshold is None:
            threshold = user.get("default_score", 70)
        return float(event.score) >= float(threshold)

    def _send_to(self, bot_token: str, chat_id: str, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=10):
                return
        except Exception as exc:
            logging.warning("Telegram alert to %s failed: %s", chat_id, exc)

    @staticmethod
    def _format(event: AnomalyEvent) -> str:
        reasons = "\n".join(f"- {reason}" for reason in event.reasons) or "- 暂无"
        suggestions = "\n".join(f"- {item}" for item in event.suggestions) or "- 继续观察"
        return (
            f"[{event.risk_level}] {event.symbol} {event.bias}\n"
            f"异常分: {event.score}/100\n"
            f"价格: {event.price}\n"
            f"1分钟: {event.price_move_pct_1m:+.3f}% | 5分钟: {event.price_move_pct_5m:+.3f}%\n"
            f"1分钟成交额: {event.quote_volume_1m:,.0f} USDT\n"
            f"OI变化: {event.oi_change_pct_5m:+.3f}% | 资金费率: {event.funding_rate:.4%}\n"
            f"多头爆仓1m: {event.long_liquidation_quote_1m:,.0f} | 空头爆仓1m: {event.short_liquidation_quote_1m:,.0f}\n"
            f"点差: {event.spread_bps:.2f} bps | 深度下降: {event.depth_drop_pct_1m:.1f}%\n"
            f"\n触发原因:\n{reasons}\n\n观察建议:\n{suggestions}"
        )
