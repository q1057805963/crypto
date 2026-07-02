import json
import logging
from urllib.request import Request, urlopen

from monitor.anomaly import AnomalyEvent


class TelegramAlert:
    def __init__(self, enabled: bool, bot_token: str, chat_ids: list[str]) -> None:
        self.requested_enabled = bool(enabled)
        self.bot_token = bot_token
        self.chat_ids = [cid.strip() for cid in chat_ids if cid.strip()]
        self.enabled = self.requested_enabled and bool(bot_token) and len(self.chat_ids) > 0

    def send(self, event: AnomalyEvent) -> None:
        if not self.enabled:
            return
        text = self._format(event)
        for chat_id in self.chat_ids:
            self._send_to(chat_id, text)

    def set_chat_ids(self, chat_ids: list[str]) -> None:
        self.chat_ids = [cid.strip() for cid in chat_ids if cid.strip()]
        self.enabled = self.requested_enabled and bool(self.bot_token) and len(self.chat_ids) > 0

    def set_config(self, enabled: bool, bot_token: str, chat_ids: list[str]) -> None:
        self.requested_enabled = bool(enabled)
        self.bot_token = bot_token
        self.chat_ids = [cid.strip() for cid in chat_ids if cid.strip()]
        self.enabled = self.requested_enabled and bool(bot_token) and len(self.chat_ids) > 0

    def _send_to(self, chat_id: str, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
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
