import asyncio
import json
import logging
import re
import threading
from time import time
from urllib.request import Request, urlopen

from monitor.rules import evaluate_trigger_rules, normalize_trigger_rules


def summarize_analysis(text: str, max_items: int = 3, max_chars: int = 110) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^\s{0,3}(?:[-*+]|(?:\d+)[.)])\s*", "", line)
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\s+", " ", line).strip(" -:：")
        if not line:
            continue
        lowered = line.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        if len(line) > max_chars:
            line = f"{line[:max_chars - 1].rstrip()}..."
        items.append(line)
        if len(items) >= max_items:
            break

    if items:
        return items

    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return []
    if len(compact) > max_chars:
        compact = f"{compact[:max_chars - 1].rstrip()}..."
    return [compact]


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

    @staticmethod
    def _cache_key(symbol: str, period: str | None = None) -> str:
        normalized_symbol = str(symbol or "").upper()
        if not period:
            return normalized_symbol
        return f"{normalized_symbol}::{period}"

    def _normalize_triggers(self, config: dict) -> dict:
        return normalize_trigger_rules(
            config.get("triggers"),
            default_score=float(config.get("activation_threshold", 60)),
            score_enabled=True,
        )

    def update_config(self, config: dict) -> None:
        self._apply(config)
        with self._lock:
            self._cache.clear()
            self._last_attempt.clear()
            self._last_error = None

    def get_cached(self, symbol: str, period: str | None = None) -> str | None:
        with self._lock:
            entry = self._cache.get(self._cache_key(symbol, period))
            if entry and time() - entry[0] < self.cache_ttl:
                return entry[1]
            return None

    def get_last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def should_activate(self, snapshot_data: dict) -> bool:
        return evaluate_trigger_rules(self.triggers, snapshot_data)

    async def analyze(
        self,
        symbol: str,
        snapshot_data: dict,
        *,
        timeframe_data: dict | None = None,
        period: str | None = None,
        force: bool = False,
    ) -> str | None:
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

        cache_key = self._cache_key(symbol, period)
        cached = self.get_cached(symbol, period)
        if cached and not force:
            return cached

        now = time()
        with self._lock:
            last_attempt = self._last_attempt.get(cache_key)
            if not force and last_attempt and now - last_attempt < self.retry_cooldown:
                self._last_error = "retry cooldown"
                return None
            self._last_attempt[cache_key] = now

        with self._lock:
            self._last_error = None
        result = await asyncio.to_thread(self._call_api, snapshot_data, timeframe_data, period)
        if result:
            with self._lock:
                self._cache[cache_key] = (time(), result)
        return result

    async def answer_question(
        self,
        question: str,
        snapshot_data: dict | None,
        available_symbols: list[str] | None = None,
    ) -> str | None:
        if not self.enabled:
            with self._lock:
                self._last_error = "ai disabled"
            return None
        if not self.api_key:
            with self._lock:
                self._last_error = "missing api key"
            return None

        with self._lock:
            self._last_error = None
        return await asyncio.to_thread(
            self._call_question_api,
            question,
            snapshot_data,
            available_symbols or [],
        )

    def _call_api(self, data: dict, timeframe_data: dict | None = None, period: str | None = None) -> str | None:
        try:
            prompt = self._build_prompt(data, timeframe_data, period)
            return self._call_prompt(prompt)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logging.warning("AI analysis failed: %s", exc)
            return None

    def _call_question_api(
        self,
        question: str,
        snapshot_data: dict | None,
        available_symbols: list[str],
    ) -> str | None:
        try:
            prompt = self._build_question_prompt(question, snapshot_data, available_symbols)
            return self._call_prompt(prompt)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logging.warning("AI question failed: %s", exc)
            return None

    def _call_prompt(self, prompt: str) -> str | None:
        if self.provider == "anthropic":
            return self._call_anthropic(prompt)
        return self._call_openai(prompt)

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

    def _build_prompt(self, data: dict, timeframe_data: dict | None = None, period: str | None = None) -> str:
        liquidation_status = {
            "recent_event": "近1分钟有强平事件",
            "no_recent_event": "数据流活跃，近1分钟无强平事件",
            "unavailable": "强平/盘口微观结构流未接入或暂不可用",
        }.get(str(data.get("liquidation_data_status") or "unavailable"), "未知")
        microstructure_status = {
            "active": "盘口/强平流活跃",
            "unavailable": "盘口/强平流不可用",
        }.get(str(data.get("microstructure_status") or "unavailable"), "未知")
        timeframe_section = ""
        period_focus = ""
        if timeframe_data:
            selected_period = str(timeframe_data.get("period_label") or period or "当前周期")
            candle_state = "已收线" if timeframe_data.get("candle_confirmed") else "进行中"
            mark_move = timeframe_data.get("mark_move_pct")
            mark_premium = timeframe_data.get("mark_premium_bps")
            mark_move_text = "--" if mark_move is None else f"{float(mark_move):+.3f}%"
            mark_premium_text = "--" if mark_premium is None else f"{float(mark_premium):+.2f} bps"
            period_liquidation_status = {
                "recent_event": "周期内有强平",
                "no_recent_event": "周期内无强平记录",
                "unavailable": "强平流不可用",
            }.get(
                str(timeframe_data.get("period_liquidation_data_status") or "unavailable"),
                "未知",
            )
            timeframe_section = (
                "\n当前选中周期数据（请作为主视角）：\n"
                f"- 分析周期: {selected_period}\n"
                f"- K线状态: {candle_state}\n"
                f"- 周期开盘: {timeframe_data.get('open_price')}\n"
                f"- 周期最高: {timeframe_data.get('high_price')}\n"
                f"- 周期最低: {timeframe_data.get('low_price')}\n"
                f"- 周期收盘/最新价: {timeframe_data.get('price')}\n"
                f"- 周期涨跌: {float(timeframe_data.get('price_move_pct', 0)):+.3f}%\n"
                f"- 相对前收: {float(timeframe_data.get('prev_close_pct', 0)):+.3f}%\n"
                f"- 周期成交额: {float(timeframe_data.get('quote_volume', 0)):,.0f} USDT\n"
                f"- 周期量能倍数: {float(timeframe_data.get('volume_multiplier', 0)):.2f}x\n"
                f"- 周期支撑: {timeframe_data.get('support_price')}\n"
                f"- 周期压力: {timeframe_data.get('resistance_price')}\n"
                f"- 周期VWAP: {timeframe_data.get('window_vwap')}\n"
                f"- 偏离VWAP: {float(timeframe_data.get('vwap_deviation_pct', 0)):+.3f}%\n"
                f"- 距支撑: {float(timeframe_data.get('support_distance_pct', 0)):.3f}%\n"
                f"- 距压力: {float(timeframe_data.get('resistance_distance_pct', 0)):.3f}%\n"
                f"- 区间位置: {float(timeframe_data.get('range_position_pct', 0)):.2f}%\n"
                f"- 标记价涨跌: {mark_move_text}\n"
                f"- 标记价偏离: {mark_premium_text}\n"
                f"- 周期强平状态: {period_liquidation_status}\n"
                f"- 周期多头爆仓: {float(timeframe_data.get('period_long_liquidation_quote', 0)):,.0f} USDT\n"
                f"- 周期空头爆仓: {float(timeframe_data.get('period_short_liquidation_quote', 0)):,.0f} USDT\n"
                f"- 周期强平事件数: {int(timeframe_data.get('period_liquidation_event_count') or 0)}\n"
            )
            period_focus = (
                f"本次分析请优先围绕 {selected_period} 周期组织判断，"
                "实时 1m / 盘口 / 强平数据只作为验证和补充，不要喧宾夺主。"
            )
        elif period:
            period_label = "实时" if period == "realtime" else period
            period_focus = f"用户当前更关心 {period_label} 档位，请尽量围绕这一档位的驱动、延续性和关键结构组织建议。"
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
            f"- 区间支撑: {data.get('support_price')}\n"
            f"- 区间压力: {data.get('resistance_price')}\n"
            f"- 区间VWAP: {data.get('window_vwap')}\n"
            f"- 偏离VWAP: {float(data.get('vwap_deviation_pct', 0)):+.3f}%\n"
            f"- 买盘墙: {data.get('bid_wall_price')} / {float(data.get('bid_wall_notional', 0)):,.0f}\n"
            f"- 卖盘墙: {data.get('ask_wall_price')} / {float(data.get('ask_wall_notional', 0)):,.0f}\n"
            f"- 触发原因: {'; '.join(data.get('reasons', []))}\n"
            f"{timeframe_section}\n"
            "请给出简洁、专业、可操作的建议（3-5条），重点关注：\n"
            "1. 当前行情的核心驱动力\n"
            "2. 短期可能的走势和风险\n"
            "3. 结合支撑/压力、VWAP、盘口墙给出具体观察位\n"
            "4. 对于杠杆交易者的具体操作建议\n"
            f"{period_focus}"
            "如果爆仓数据状态不可用，不要把爆仓金额为0解读为没有强平压力。"
            "直接给出建议，不要重复数据。每条建议控制在一句话。"
        )

    def _build_question_prompt(
        self,
        question: str,
        data: dict | None,
        available_symbols: list[str],
    ) -> str:
        if not data:
            symbols_text = ", ".join(available_symbols[:30]) or "暂无"
            return (
                f"用户通过 Telegram Bot 提问：{question}\n\n"
                f"当前可查询合约：{symbols_text}\n"
                "请用中文简洁回复，提醒用户需要带上明确合约，例如 BTC、ETH、SOL。"
            )

        liquidation_status = {
            "recent_event": "近1分钟有强平事件",
            "no_recent_event": "强平数据已接入，近1分钟未捕获强平订单",
            "unavailable": "强平数据不可用",
        }.get(str(data.get("liquidation_data_status") or "unavailable"), "未知")
        return (
            f"用户通过 Telegram Bot 针对 {data.get('symbol')} 提问：{question}\n\n"
            "以下是该 USDT 永续合约的当前监控快照：\n"
            f"- 当前价格: {data.get('price')}\n"
            f"- 异常分: {data.get('score', 0)}/100\n"
            f"- 风险等级: {data.get('risk_level', '低风险')}\n"
            f"- 当前倾向: {data.get('bias', '')}\n"
            f"- 1分钟波动: {float(data.get('price_move_pct_1m', 0)):+.3f}%\n"
            f"- 5分钟波动: {float(data.get('price_move_pct_5m', 0)):+.3f}%\n"
            f"- 1分钟成交额: {float(data.get('quote_volume_1m', 0)):,.0f} USDT\n"
            f"- 量能倍数: {float(data.get('volume_multiplier', 0)):.2f}x\n"
            f"- 主动买入占比: {float(data.get('taker_buy_ratio_1m', 0.5)):.1%}\n"
            f"- OI 5分钟变化: {float(data.get('oi_change_pct_5m', 0)):+.3f}%\n"
            f"- 资金费率: {float(data.get('funding_rate', 0)):.4%}\n"
            f"- 爆仓状态: {liquidation_status}\n"
            f"- 多头爆仓1m: {float(data.get('long_liquidation_quote_1m', 0)):,.0f} USDT\n"
            f"- 空头爆仓1m: {float(data.get('short_liquidation_quote_1m', 0)):,.0f} USDT\n"
            f"- 点差: {float(data.get('spread_bps', 0)):.2f} bps\n"
            f"- 盘口深度下降: {float(data.get('depth_drop_pct_1m', 0)):.1f}%\n"
            f"- 区间支撑 / 压力: {data.get('support_price')} / {data.get('resistance_price')}\n"
            f"- 区间VWAP: {data.get('window_vwap')}\n"
            f"- 买盘墙 / 卖盘墙: {data.get('bid_wall_price')} / {data.get('ask_wall_price')}\n"
            f"- 触发原因: {'; '.join(data.get('reasons', [])) or '暂无'}\n\n"
            "请直接回答用户问题，控制在 4 条以内。"
            "重点给出上涨/下跌倾向、主要依据、风险等级和接下来观察点，尽量指出支撑/压力或关键价位。"
            "不要承诺收益，不要给绝对买卖指令；如果数据不足，要明确说数据不足。"
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
            text = self._extract_text(data)
            if not text:
                raise ValueError(f"missing text in AI response keys: {sorted(data.keys())}")
            return text

    @staticmethod
    def _extract_text(data: dict) -> str | None:
        def collect(value, parts: list[str]) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(text)
                return
            if isinstance(value, list):
                for item in value:
                    collect(item, parts)
                return
            if not isinstance(value, dict):
                return

            choices = value.get("choices")
            if isinstance(choices, list):
                collect(choices, parts)
            message = value.get("message")
            if isinstance(message, (dict, list, str)):
                collect(message, parts)
            content = value.get("content")
            if isinstance(content, (dict, list, str)):
                collect(content, parts)
            for key in ("text", "output_text", "message", "output"):
                item = value.get(key)
                if isinstance(item, (dict, list, str)):
                    collect(item, parts)

        parts: list[str] = []
        collect(data, parts)
        if not parts:
            return None
        deduped = []
        seen = set()
        for part in parts:
            if part in seen:
                continue
            deduped.append(part)
            seen.add(part)
        return "\n".join(deduped)

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
