import asyncio
import json
import logging
import threading
from time import time
from urllib.request import Request, urlopen


class AIAnalyzer:
    def __init__(self, config: dict) -> None:
        self._apply(config)
        self._cache: dict[str, tuple[float, str]] = {}
        self._last_attempt: dict[str, float] = {}
        self._last_error: str | None = None
        self._lock = threading.Lock()

    def _apply(self, config: dict) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.provider = config.get("provider", "openai")
        self.api_key = str(config.get("api_key", ""))
        self.model = str(config.get("model", "gpt-4o-mini"))
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.activation_threshold = float(config.get("activation_threshold", 60))
        self.triggers = self._normalize_triggers(config)
        self.max_tokens = int(config.get("max_tokens", 500))
        self.cache_ttl = int(config.get("cache_ttl_seconds", 300))
        self.retry_cooldown = int(config.get("retry_cooldown_seconds", 120))
        self.request_timeout = int(config.get("request_timeout_seconds", 30))

    def _normalize_triggers(self, config: dict) -> dict:
        triggers = dict(config.get("triggers") or {})
        conditions = dict(triggers.get("conditions") or {})
        defaults = {
            "score": {"enabled": True, "threshold": float(config.get("activation_threshold", 60))},
            "quote_volume_1m": {"enabled": False, "threshold": 500000},
            "volume_multiplier": {"enabled": False, "threshold": 3},
            "price_move_pct_1m_abs": {"enabled": False, "threshold": 0.8},
            "oi_change_pct_5m_abs": {"enabled": False, "threshold": 1.5},
            "liquidation_total_quote_1m": {"enabled": False, "threshold": 250000},
        }
        normalized = {}
        for key, default in defaults.items():
            value = dict(conditions.get(key) or {})
            normalized[key] = {
                "enabled": bool(value.get("enabled", default["enabled"])),
                "threshold": float(value.get("threshold", default["threshold"])),
            }
        return {
            "mode": "all" if triggers.get("mode") == "all" else "any",
            "conditions": normalized,
        }

    def update_config(self, config: dict) -> None:
        self._apply(config)
        with self._lock:
            self._cache.clear()
            self._last_attempt.clear()
            self._last_error = None

    def get_cached(self, symbol: str) -> str | None:
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and time() - entry[0] < self.cache_ttl:
                return entry[1]
            return None

    def get_last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def should_activate(self, snapshot_data: dict) -> bool:
        checks = []
        conditions = self.triggers.get("conditions", {})
        for key, cfg in conditions.items():
            if not cfg.get("enabled"):
                continue
            threshold = float(cfg.get("threshold", 0))
            if key == "score":
                value = float(snapshot_data.get("score", 0) or 0)
            elif key == "quote_volume_1m":
                value = float(snapshot_data.get("quote_volume_1m", 0) or 0)
            elif key == "volume_multiplier":
                value = float(snapshot_data.get("volume_multiplier", 0) or 0)
            elif key == "price_move_pct_1m_abs":
                value = abs(float(snapshot_data.get("price_move_pct_1m", 0) or 0))
            elif key == "oi_change_pct_5m_abs":
                value = abs(float(snapshot_data.get("oi_change_pct_5m", 0) or 0))
            elif key == "liquidation_total_quote_1m":
                value = float(snapshot_data.get("liquidation_total_quote_1m", 0) or 0)
            else:
                continue
            checks.append(value >= threshold)

        if not checks:
            return False
        if self.triggers.get("mode") == "all":
            return all(checks)
        return any(checks)

    async def analyze(self, symbol: str, snapshot_data: dict, force: bool = False) -> str | None:
        if not self.enabled:
            with self._lock:
                self._last_error = "ai disabled"
            return None
        if not self.api_key:
            with self._lock:
                self._last_error = "missing api key"
            return None

        if not force and not self.should_activate(snapshot_data):
            with self._lock:
                self._last_error = "ai trigger not met"
            return None

        cached = self.get_cached(symbol)
        if cached and not force:
            return cached

        now = time()
        with self._lock:
            last_attempt = self._last_attempt.get(symbol)
            if not force and last_attempt and now - last_attempt < self.retry_cooldown:
                self._last_error = "retry cooldown"
                return None
            self._last_attempt[symbol] = now

        with self._lock:
            self._last_error = None
        result = await asyncio.to_thread(self._call_api, snapshot_data)
        if result:
            with self._lock:
                self._cache[symbol] = (time(), result)
        return result

    def _call_api(self, data: dict) -> str | None:
        try:
            prompt = self._build_prompt(data)
            if self.provider == "anthropic":
                return self._call_anthropic(prompt)
            return self._call_openai(prompt)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logging.warning("AI analysis failed: %s", exc)
            return None

    @staticmethod
    def _provider_defaults() -> dict[str, str]:
        return {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com",
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "siliconflow": "https://api.siliconflow.cn/v1",
            "moonshot": "https://api.moonshot.cn/v1",
            "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "custom": "",
        }

    def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return self._provider_defaults().get(self.provider, "https://api.openai.com/v1").rstrip("/")

    def _openai_endpoint(self) -> str:
        base = self._resolve_base_url() or "https://api.openai.com/v1"
        return f"{base}/chat/completions"

    def _build_prompt(self, data: dict) -> str:
        liquidation_status = {
            "recent_event": "近1分钟有强平事件",
            "no_recent_event": "数据流活跃，近1分钟无强平事件",
            "unavailable": "强平/盘口微观结构流未接入或暂不可用",
        }.get(str(data.get("liquidation_data_status") or "unavailable"), "未知")
        microstructure_status = {
            "active": "盘口/强平流活跃",
            "unavailable": "盘口/强平流不可用",
        }.get(str(data.get("microstructure_status") or "unavailable"), "未知")
        return (
            f"以下是 {data['symbol']} USDT永续合约的实时监控数据：\n"
            f"- 异常分: {data.get('score', 0)}/100\n"
            f"- 风险等级: {data.get('risk_level', '低风险')}\n"
            f"- 倾向: {data.get('bias', '')}\n"
            f"- 价格: {data.get('price')}\n"
            f"- 1分钟波动: {float(data.get('price_move_pct_1m', 0)):+.3f}%\n"
            f"- 5分钟波动: {float(data.get('price_move_pct_5m', 0)):+.3f}%\n"
            f"- 1分钟成交额: {float(data.get('quote_volume_1m', 0)):,.0f} USDT\n"
            f"- 量能倍数: {float(data.get('volume_multiplier', 0)):.2f}x\n"
            f"- OI 5分钟变化: {float(data.get('oi_change_pct_5m', 0)):+.3f}%\n"
            f"- 资金费率: {float(data.get('funding_rate', 0)):.4%}\n"
            f"- 微观结构状态: {microstructure_status}\n"
            f"- 爆仓数据状态: {liquidation_status}\n"
            f"- 强平事件数1m: {int(data.get('liquidation_event_count_1m') or 0)}\n"
            f"- 多头爆仓1m: {float(data.get('long_liquidation_quote_1m', 0)):,.0f}\n"
            f"- 空头爆仓1m: {float(data.get('short_liquidation_quote_1m', 0)):,.0f}\n"
            f"- 盘口点差: {float(data.get('spread_bps', 0)):.2f} bps\n"
            f"- 深度下降: {float(data.get('depth_drop_pct_1m', 0)):.1f}%\n"
            f"- 触发原因: {'; '.join(data.get('reasons', []))}\n\n"
            "请给出简洁、专业、可操作的建议（3-5条），重点关注：\n"
            "1. 当前行情的核心驱动力\n"
            "2. 短期可能的走势和风险\n"
            "3. 对于杠杆交易者的具体操作建议\n"
            "如果爆仓数据状态不可用，不要把爆仓金额为0解读为没有强平压力。"
            "直接给出建议，不要重复数据。每条建议控制在一句话。"
        )

    def _system_prompt(self) -> str:
        return (
            "你是一名加密货币合约交易风险分析专家，精通USDT永续合约、"
            "杠杆交易策略、资金费率套利、持仓量分析、盘口微观结构解读、"
            "以及多空博弈下的风险评估。你的分析简洁专业，直击要害。"
        )

    def _call_openai(self, prompt: str) -> str | None:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.3,
        }).encode("utf-8")
        req = Request(
            self._openai_endpoint(),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_text(data: dict) -> str | None:
        if not isinstance(data, dict):
            return None

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content

        content = data.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    for key in ("text", "content", "message"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            parts.append(value)
                            break
            if parts:
                return "\n".join(parts)

        for key in ("text", "message", "output"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return None

    def _call_anthropic(self, prompt: str) -> str | None:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._system_prompt(),
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        base = self.base_url.rstrip("/") if self.base_url else "https://api.anthropic.com"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        if "api.anthropic.com" not in base:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(
            f"{base}/v1/messages",
            data=payload,
            headers=headers,
        )
        with urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read())
            text = self._extract_text(data)
            if not text:
                raise ValueError(f"missing text in AI response keys: {sorted(data.keys())}")
            return text
