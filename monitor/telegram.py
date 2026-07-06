import json
import logging
from dataclasses import asdict
from time import time
from urllib.request import Request, urlopen

from monitor.anomaly import AnomalyEvent
from monitor.rules import (
    enabled_trigger_count,
    evaluate_trigger_rules,
    metric_value,
    normalize_trigger_rules,
)


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
                "default_score": float(user.get("default_score", 60)),
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
                "default_score": 60.0,
            }
        )
    return normalized


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> str | None:
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
            return None
    except Exception as exc:
        return str(exc)


def send_telegram_chat_action(bot_token: str, chat_id: str, action: str = "typing") -> str | None:
    payload = json.dumps({"chat_id": chat_id, "action": action}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendChatAction",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10):
            return None
    except Exception as exc:
        return str(exc)


def send_text_to_telegram_users(users: list[dict], text: str) -> dict:
    sent = 0
    errors = []
    for user in normalize_telegram_users(users):
        if not TelegramAlert._user_ready(user):
            continue
        for chat_id in user["chat_ids"]:
            error = send_telegram_message(user["bot_token"], chat_id, text)
            if error:
                errors.append({"chat_id": _mask_chat_id(chat_id), "error": error})
            else:
                sent += 1
    return {"sent": sent, "errors": errors}


def _mask_chat_id(chat_id: str) -> str:
    value = str(chat_id)
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


class TelegramAlert:
    def __init__(
        self,
        enabled: bool,
        bot_token: str = "",
        chat_ids: list[str] | None = None,
        users: list[dict] | None = None,
        cooldown_seconds: float = 120,
    ) -> None:
        self.requested_enabled = bool(enabled)
        self.users = normalize_telegram_users(users, bot_token, chat_ids or [])
        self.cooldown_seconds = float(cooldown_seconds)
        self._last_sent_at: dict[str, float] = {}
        self.enabled = self.requested_enabled and any(self._user_ready(user) for user in self.users)

    def send(self, event: AnomalyEvent) -> None:
        if not self.enabled:
            return
        payload = asdict(event)
        payload["updated_at"] = time()
        self._send_payload(payload)

    def send_snapshot(self, snapshot: dict) -> None:
        if not self.enabled or not snapshot:
            return
        self._send_payload(snapshot)

    def has_ready_targets(self, snapshot: dict) -> bool:
        if not self.enabled or not snapshot:
            return False
        symbol = str(snapshot.get("symbol", "")).upper()
        if not symbol:
            return False
        event_time = float(snapshot.get("updated_at") or time())
        for user in self.users:
            if not self._user_ready(user):
                continue
            if not self._user_wants_snapshot(user, snapshot):
                continue
            for chat_id in user["chat_ids"]:
                if self._can_send(user["bot_token"], chat_id, symbol, event_time):
                    return True
        return False

    def _send_payload(self, snapshot: dict) -> None:
        symbol = str(snapshot.get("symbol", "")).upper()
        if not symbol:
            return
        event_time = float(snapshot.get("updated_at") or time())
        for user in self.users:
            if not self._user_ready(user):
                continue
            if not self._user_wants_snapshot(user, snapshot):
                continue
            text = self._format_snapshot(snapshot, user)
            for chat_id in user["chat_ids"]:
                if not self._can_send(user["bot_token"], chat_id, symbol, event_time):
                    continue
                if self._send_to(user["bot_token"], chat_id, text):
                    self._mark_sent(user["bot_token"], chat_id, symbol, event_time)

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
        return TelegramAlert._user_wants_snapshot(user, asdict(event))

    @staticmethod
    def _user_wants_snapshot(user: dict, snapshot: dict) -> bool:
        symbols = {str(symbol).upper() for symbol in user.get("symbols", [])}
        symbol = str(snapshot.get("symbol", "")).upper()
        if symbols and symbol not in symbols:
            return False
        symbol_thresholds = user.get("symbol_thresholds") or {}
        symbol_config = (
            symbol_thresholds.get(symbol, {})
            if isinstance(symbol_thresholds, dict)
            else {}
        )
        push_rules = symbol_config.get("push_rules") if isinstance(symbol_config, dict) else None
        if enabled_trigger_count(push_rules) > 0:
            return evaluate_trigger_rules(push_rules, snapshot)
        threshold = symbol_config.get("anomaly_score") if isinstance(symbol_config, dict) else None
        if threshold is None:
            threshold = user.get("default_score", 60)
        return float(snapshot.get("score", 0) or 0) >= float(threshold)

    def _can_send(self, bot_token: str, chat_id: str, symbol: str, event_time: float) -> bool:
        last_sent_at = self._last_sent_at.get(self._cooldown_key(bot_token, chat_id, symbol), 0.0)
        return event_time - last_sent_at >= self.cooldown_seconds

    def _mark_sent(self, bot_token: str, chat_id: str, symbol: str, event_time: float) -> None:
        self._last_sent_at[self._cooldown_key(bot_token, chat_id, symbol)] = event_time

    @staticmethod
    def _cooldown_key(bot_token: str, chat_id: str, symbol: str) -> str:
        return f"{bot_token}:{chat_id}:{symbol.upper()}"

    def _send_to(self, bot_token: str, chat_id: str, text: str) -> bool:
        error = send_telegram_message(bot_token, chat_id, text)
        if error:
            logging.warning("Telegram alert to %s failed: %s", chat_id, error)
            return False
        return True

    @staticmethod
    def _format_snapshot(snapshot: dict, user: dict | None = None) -> str:
        reasons = "\n".join(f"- {reason}" for reason in snapshot.get("reasons", [])) or "- 暂无"
        suggestions = "\n".join(f"- {item}" for item in snapshot.get("suggestions", [])) or "- 继续观察"
        trigger_context = TelegramAlert._trigger_context(snapshot, user)
        ai_summary = [
            str(item).strip()
            for item in snapshot.get("ai_summary", [])
            if str(item).strip()
        ]
        ai_analysis = str(snapshot.get("ai_analysis", "") or "").strip()
        ai_block = ""
        if ai_summary:
            ai_block = "\n\nAI分析:\n" + "\n".join(f"- {item}" for item in ai_summary)
        elif ai_analysis:
            ai_block = f"\n\nAI分析:\n- {ai_analysis}"
        support = float(snapshot.get("support_price", 0) or 0)
        resistance = float(snapshot.get("resistance_price", 0) or 0)
        bid_wall_price = float(snapshot.get("bid_wall_price", 0) or 0)
        ask_wall_price = float(snapshot.get("ask_wall_price", 0) or 0)
        support_text = f"{support:.8f}" if support > 0 else "--"
        resistance_text = f"{resistance:.8f}" if resistance > 0 else "--"
        bid_wall_text = f"{bid_wall_price:.8f}" if bid_wall_price > 0 else "--"
        ask_wall_text = f"{ask_wall_price:.8f}" if ask_wall_price > 0 else "--"
        return (
            f"[{snapshot.get('risk_level', '风险提示')}] {snapshot.get('symbol', '')} {snapshot.get('bias', '')}\n"
            f"异常分: {float(snapshot.get('score', 0) or 0):.1f}/100\n"
            f"价格: {snapshot.get('price')}\n"
            f"1分钟: {float(snapshot.get('price_move_pct_1m', 0) or 0):+.3f}% | 5分钟: {float(snapshot.get('price_move_pct_5m', 0) or 0):+.3f}%\n"
            f"1分钟成交额: {float(snapshot.get('quote_volume_1m', 0) or 0):,.0f} USDT\n"
            f"OI变化: {float(snapshot.get('oi_change_pct_5m', 0) or 0):+.3f}% | 资金费率: {float(snapshot.get('funding_rate', 0) or 0):.4%}\n"
            f"区间支撑: {support_text} | 区间压力: {resistance_text}\n"
            f"买盘墙: {bid_wall_text} | 卖盘墙: {ask_wall_text}\n"
            f"多头爆仓1m: {float(snapshot.get('long_liquidation_quote_1m', 0) or 0):,.0f} | 空头爆仓1m: {float(snapshot.get('short_liquidation_quote_1m', 0) or 0):,.0f}\n"
            f"点差: {float(snapshot.get('spread_bps', 0) or 0):.2f} bps | 深度下降: {float(snapshot.get('depth_drop_pct_1m', 0) or 0):.1f}%\n"
            f"\n推送条件:\n{trigger_context}\n\n触发原因:\n{reasons}\n\n观察建议:\n{suggestions}{ai_block}"
        )

    @staticmethod
    def _trigger_context(snapshot: dict, user: dict | None = None) -> str:
        user = user or {}
        symbol = str(snapshot.get("symbol", "")).upper()
        symbol_thresholds = user.get("symbol_thresholds") or {}
        symbol_config = (
            symbol_thresholds.get(symbol, {})
            if isinstance(symbol_thresholds, dict)
            else {}
        )
        default_score = float(user.get("default_score", 60) or 60)
        threshold = (
            float(symbol_config.get("anomaly_score"))
            if isinstance(symbol_config, dict) and symbol_config.get("anomaly_score") is not None
            else default_score
        )
        push_rules = symbol_config.get("push_rules") if isinstance(symbol_config, dict) else None
        if enabled_trigger_count(push_rules) <= 0:
            source = "单币阈值" if isinstance(symbol_config, dict) and symbol_config.get("anomaly_score") is not None else "默认阈值"
            return f"- {source}: 异常分 {float(snapshot.get('score', 0) or 0):.1f} >= {threshold:.1f}"

        normalized = normalize_trigger_rules(
            push_rules,
            default_score=threshold,
            score_enabled=False,
        )
        mode_text = "全部附加条件满足" if normalized.get("mode") == "all" else "任一附加条件满足"
        passed = []
        missed = []
        for key, cfg in normalized.get("conditions", {}).items():
            if not cfg.get("enabled"):
                continue
            value = metric_value(key, snapshot)
            if value is None:
                continue
            threshold_value = float(cfg.get("threshold", 0) or 0)
            text = TelegramAlert._condition_text(key, value, threshold_value)
            if value >= threshold_value:
                passed.append(text)
            else:
                missed.append(text)

        lines = [f"- 推送规则: {mode_text}"]
        if passed:
            lines.append("- 命中: " + "；".join(passed[:5]))
        if normalized.get("mode") == "all" and missed:
            lines.append("- 未满足: " + "；".join(missed[:3]))
        if not passed and missed:
            lines.append("- 条件: " + "；".join(missed[:5]))
        return "\n".join(lines)

    @staticmethod
    def _condition_text(key: str, value: float, threshold: float) -> str:
        labels = {
            "score": ("异常分", "", 1),
            "quote_volume_1m": ("1分钟成交额", " USDT", 0),
            "volume_multiplier": ("量能倍数", "x", 2),
            "price_move_pct_1m_abs": ("1分钟波动", "%", 2),
            "oi_change_pct_5m_abs": ("OI 5分钟", "%", 2),
            "liquidation_total_quote_1m": ("1分钟爆仓额", " USDT", 0),
            "depth_imbalance_abs": ("盘口失衡", "%", 1),
            "depth_drop_pct_1m": ("深度下降", "%", 1),
            "spread_bps": ("盘口点差", " bps", 2),
        }
        label, unit, digits = labels.get(key, (key, "", 2))
        if unit == " USDT":
            value_text = f"{value:,.0f}{unit}"
            threshold_text = f"{threshold:,.0f}{unit}"
        else:
            value_text = f"{value:.{digits}f}{unit}"
            threshold_text = f"{threshold:.{digits}f}{unit}"
        return f"{label} {value_text} >= {threshold_text}"
