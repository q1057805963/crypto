import json
import logging
from urllib.request import Request, urlopen

from monitor.anomaly import AnomalyEvent


class TelegramAlert:
    def __init__(self, enabled: bool, bot_token: str, chat_id: str) -> None:
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, event: AnomalyEvent) -> None:
        if not self.enabled:
            return

        text = self._format(event)
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
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
            logging.warning("Telegram alert failed: %s", exc)

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
            f"\n触发原因:\n{reasons}\n\n观察建议:\n{suggestions}"
        )
