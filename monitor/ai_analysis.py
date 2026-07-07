import asyncio
import json
import logging
import re
import threading
from time import time
from urllib.request import Request, urlopen

from monitor.rules import evaluate_trigger_rules, normalize_trigger_rules, trigger_rule_status


AI_ANALYSIS_SCHEMA_VERSION = "scenario-v3"


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
        self.max_tokens = int(config.get("max_tokens", 1000))
        self.question_max_tokens = int(
            config.get("question_max_tokens", max(self.max_tokens, 700))
        )
        self.analysis_temperature = float(config.get("temperature", 0.3))
        self.question_temperature = float(config.get("question_temperature", 0.55))
        self.cache_ttl = int(config.get("cache_ttl_seconds", 300))
        self.retry_cooldown = int(config.get("retry_cooldown_seconds", 120))
        self.request_timeout = int(config.get("request_timeout_seconds", 30))

    @staticmethod
    def _cache_key(symbol: str, period: str | None = None) -> str:
        normalized_symbol = str(symbol or "").upper()
        if not period:
            return f"{normalized_symbol}::{AI_ANALYSIS_SCHEMA_VERSION}"
        return f"{normalized_symbol}::{period}::{AI_ANALYSIS_SCHEMA_VERSION}"

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

    def trigger_status(self, snapshot_data: dict) -> dict:
        return trigger_rule_status(self.triggers, snapshot_data)

    async def analyze(
        self,
        symbol: str,
        snapshot_data: dict,
        *,
        timeframe_data: dict | None = None,
        confluence_data: dict | None = None,
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
        result = await asyncio.to_thread(
            self._call_api,
            snapshot_data,
            timeframe_data,
            confluence_data,
            period,
        )
        if result:
            with self._lock:
                self._cache[cache_key] = (time(), result)
        return result

    async def answer_question(
        self,
        question: str,
        snapshot_data: dict | None,
        available_symbols: list[str] | None = None,
        *,
        history: list[dict] | None = None,
        timeframe_data: dict | None = None,
        confluence_data: dict | None = None,
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
            history or [],
            timeframe_data,
            confluence_data,
        )

    def _call_api(
        self,
        data: dict,
        timeframe_data: dict | None = None,
        confluence_data: dict | None = None,
        period: str | None = None,
    ) -> str | None:
        try:
            prompt = self._build_prompt(data, timeframe_data, period, confluence_data)
            return self._call_prompt(
                prompt,
                system_prompt=self._system_prompt(),
                temperature=self.analysis_temperature,
                max_tokens=self.max_tokens,
            )
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
        history: list[dict] | None = None,
        timeframe_data: dict | None = None,
        confluence_data: dict | None = None,
    ) -> str | None:
        try:
            prompt = self._build_question_prompt(
                question,
                snapshot_data,
                available_symbols,
                history=history,
                timeframe_data=timeframe_data,
                confluence_data=confluence_data,
            )
            return self._call_prompt(
                prompt,
                system_prompt=self._question_system_prompt(),
                temperature=self.question_temperature,
                max_tokens=self.question_max_tokens,
            )
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logging.warning("AI question failed: %s", exc)
            return None

    def _call_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str | None:
        if self.provider == "anthropic":
            return self._call_anthropic(prompt, system_prompt, temperature, max_tokens)
        return self._call_openai(prompt, system_prompt, temperature, max_tokens)

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

    @staticmethod
    def _stats_section(data: dict) -> str:
        trigger_combo = data.get("trigger_combo") if isinstance(data.get("trigger_combo"), dict) else {}
        signal_stats = data.get("signal_stats") if isinstance(data.get("signal_stats"), dict) else {}
        combo_stats = data.get("combo_stats") if isinstance(data.get("combo_stats"), dict) else {}
        lines = []
        if trigger_combo:
            lines.append(f"- 当前触发组合: {trigger_combo.get('label') or trigger_combo.get('key')}")
        if signal_stats:
            lines.append(
                "- 同币同向后效: "
                f"{signal_stats.get('label', '15m')} 样本 {int(signal_stats.get('sample_count') or 0)}, "
                f"胜率 {float(signal_stats.get('win_rate') or 0):.1f}%, "
                f"均值 {float(signal_stats.get('avg_close_bps') or 0):+.1f}bp, "
                f"顺向 {float(signal_stats.get('avg_favorable_bps') or 0):.1f}bp, "
                f"逆向 {float(signal_stats.get('avg_adverse_bps') or 0):.1f}bp, "
                f"可信度 {signal_stats.get('reliability', 'low')}"
            )
        if combo_stats:
            lines.append(
                "- 同组合后效: "
                f"{combo_stats.get('label', '15m')} 样本 {int(combo_stats.get('sample_count') or 0)}, "
                f"胜率 {float(combo_stats.get('win_rate') or 0):.1f}%, "
                f"均值 {float(combo_stats.get('avg_close_bps') or 0):+.1f}bp, "
                f"顺向 {float(combo_stats.get('avg_favorable_bps') or 0):.1f}bp, "
                f"逆向 {float(combo_stats.get('avg_adverse_bps') or 0):.1f}bp, "
                f"可信度 {combo_stats.get('reliability', 'low')}"
            )
        if not lines:
            lines.append("- 当前信号暂无足够后效样本，不能把单次异动当成高胜率信号。")
        return "\n信号后效统计（必须约束结论强度）：\n" + "\n".join(lines) + "\n"

    @staticmethod
    def _confluence_section(confluence_data: dict | None) -> str:
        if not confluence_data:
            return "\n多周期共振：暂不可用，请不要声称多周期已经确认。\n"
        period_lines = []
        for item in confluence_data.get("periods") or []:
            period_lines.append(
                f"- {item.get('period_label') or item.get('period')}: "
                f"{item.get('structure_label') or item.get('structure_regime')} / "
                f"偏向={item.get('bias')} / "
                f"涨跌 {float(item.get('price_move_pct') or 0):+.2f}% / "
                f"量能 {float(item.get('volume_multiplier') or 0):.2f}x / "
                f"VWAP偏离 {float(item.get('vwap_deviation_pct') or 0):+.2f}%"
            )
        confirmations = "；".join(str(item) for item in confluence_data.get("confirmations") or []) or "暂无明确同向确认"
        conflicts = "；".join(str(item) for item in confluence_data.get("conflicts") or []) or "暂无明显冲突"
        return (
            "\n多周期共振（用于判断是否只是短线噪音）：\n"
            f"- 结论: {confluence_data.get('label')} / 方向={confluence_data.get('direction')} / "
            f"共振分={float(confluence_data.get('score') or 0):.1f}/100\n"
            f"- 摘要: {confluence_data.get('summary') or ''}\n"
            f"- 同向证据: {confirmations}\n"
            f"- 冲突证据: {conflicts}\n"
            + "\n".join(period_lines)
            + "\n"
        )

    def _build_alert_prompt(self, data: dict) -> str:
        liquidation_status = {
            "recent_event": "近1分钟有强平事件",
            "no_recent_event": "数据流活跃，近1分钟无强平事件",
            "unavailable": "强平/盘口微观结构流未接入或暂不可用",
        }.get(str(data.get("liquidation_data_status") or "unavailable"), "未知")
        return (
            f"{data['symbol']} USDT永续合约刚触发异动告警，即将推送到 Telegram。数据如下：\n"
            f"- 异常分: {data.get('score', 0)}/100 | 风险等级: {data.get('risk_level', '低风险')} | 倾向: {data.get('bias', '')}\n"
            f"- 价格: {data.get('price')}\n"
            f"- 1分钟波动: {float(data.get('price_move_pct_1m', 0)):+.3f}% | 5分钟波动: {float(data.get('price_move_pct_5m', 0)):+.3f}%\n"
            f"- 1分钟成交额: {float(data.get('quote_volume_1m', 0)):,.0f} USDT | 量能倍数: {float(data.get('volume_multiplier', 0)):.2f}x\n"
            f"- 主动买入占比: {float(data.get('taker_buy_ratio_1m', 0.5)):.1%}\n"
            f"- OI 5分钟变化: {float(data.get('oi_change_pct_5m', 0)):+.3f}% | 资金费率: {float(data.get('funding_rate', 0)):.4%}\n"
            f"- 爆仓数据状态: {liquidation_status}\n"
            f"- 多头爆仓1m: {float(data.get('long_liquidation_quote_1m', 0)):,.0f} | 空头爆仓1m: {float(data.get('short_liquidation_quote_1m', 0)):,.0f}\n"
            f"- 盘口点差: {float(data.get('spread_bps', 0)):.2f} bps | 深度下降: {float(data.get('depth_drop_pct_1m', 0)):.1f}% | 盘口失衡: {float(data.get('depth_imbalance', 0)):+.3f}\n"
            f"- 区间支撑/压力: {data.get('support_price')} / {data.get('resistance_price')}\n"
            f"- 区间VWAP: {data.get('window_vwap')} (偏离 {float(data.get('vwap_deviation_pct', 0)):+.3f}%)\n"
            f"- 买盘墙: {data.get('bid_wall_price')} ({float(data.get('bid_wall_notional', 0)):,.0f}) | "
            f"卖盘墙: {data.get('ask_wall_price')} ({float(data.get('ask_wall_notional', 0)):,.0f})\n"
            f"- 触发原因: {'; '.join(data.get('reasons', []))}\n"
            f"{self._stats_section(data)}"
            "请为这条告警生成『观察建议』，要求：\n"
            "- 输出 3-5 行，每行一条独立建议，短横线开头，Telegram 纯文本，不用 Markdown。\n"
            "- 第一条先定性：这次异动更像什么（如多头踩踏、主动出货、逼空、假突破、试盘），以及接下来更可能的路径。\n"
            "- 建议必须绑定本次数据里的具体数字：至少引用支撑/压力/盘口墙/VWAP 中的一个具体价位，"
            "并结合量能、主动成交、OI 或爆仓中的至少两项来支撑判断，不要写放之四海而皆准的套话。\n"
            "- 必须有一条给出明确的失效/反转确认条件（具体价位或行为）。\n"
            "- 结论强度受后效统计约束，样本不足或可信度 low 时要明说；"
            "爆仓数据不可用时，不要把爆仓金额 0 解读为没有强平压力。\n"
            "- 每行控制在 60 字以内，不给绝对买卖指令。"
        )

    def _build_prompt(
        self,
        data: dict,
        timeframe_data: dict | None = None,
        period: str | None = None,
        confluence_data: dict | None = None,
    ) -> str:
        if period == "alert" and not timeframe_data:
            return self._build_alert_prompt(data)
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
        stats_section = self._stats_section(data)
        confluence_section = self._confluence_section(confluence_data)
        if timeframe_data:
            selected_period = str(timeframe_data.get("period_label") or period or "当前周期")
            candle_state = "已收线" if timeframe_data.get("candle_confirmed") else "进行中"
            support_source = str(timeframe_data.get("support_source") or "unknown")
            resistance_source = str(timeframe_data.get("resistance_source") or "unknown")
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
                f"- 周期成交额: {float(timeframe_data.get('quote_volume', 0)):,.0f} USDT\n"
                f"- 周期量能倍数: {float(timeframe_data.get('volume_multiplier', 0)):.2f}x\n"
                f"- 结构支撑: {timeframe_data.get('support_price')} "
                f"(来源={support_source}, 触碰={int(timeframe_data.get('support_touch_count') or 0)}, "
                f"摆动点={int(timeframe_data.get('support_pivot_count') or 0)}, "
                f"强度={float(timeframe_data.get('support_strength') or 0):.2f}, "
                f"评分={float(timeframe_data.get('support_confluence_score') or 0):.1f}, "
                f"状态={timeframe_data.get('support_status') or 'unknown'})\n"
                f"- 结构压力: {timeframe_data.get('resistance_price')} "
                f"(来源={resistance_source}, 触碰={int(timeframe_data.get('resistance_touch_count') or 0)}, "
                f"摆动点={int(timeframe_data.get('resistance_pivot_count') or 0)}, "
                f"强度={float(timeframe_data.get('resistance_strength') or 0):.2f}, "
                f"评分={float(timeframe_data.get('resistance_confluence_score') or 0):.1f}, "
                f"状态={timeframe_data.get('resistance_status') or 'unknown'})\n"
                f"- 结构状态: {timeframe_data.get('structure_regime') or 'unknown'}\n"
                f"- 成交密集 POC: {timeframe_data.get('profile_poc_price')} "
                f"({float(timeframe_data.get('profile_poc_quote_volume') or 0):,.0f} USDT)\n"
                f"- 价值区间: {timeframe_data.get('value_area_low')} / {timeframe_data.get('value_area_high')}\n"
                f"- 成交密集支撑/压力: {timeframe_data.get('support_profile_price')} / {timeframe_data.get('resistance_profile_price')}\n"
                f"- 阶段最低/最高: {timeframe_data.get('period_low_price')} / {timeframe_data.get('period_high_price')}\n"
                f"- 结构样本: {int(timeframe_data.get('structure_sample_count') or 0)} 根K线, "
                f"聚类容差 {float(timeframe_data.get('structure_tolerance_pct') or 0):.3f}%\n"
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
                "支撑/压力必须使用结构支撑/结构压力字段；阶段最低/最高仅用于说明区间边界，"
                "不要把旧的单根最高最低当作主支撑压力。"
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
            f"- 实时标记价: {data.get('mark_price') or '--'}\n"
            f"- 实时标记价偏离: {float(data.get('mark_premium_bps', 0)):+.2f} bps\n"
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
            f"{stats_section}"
            f"{confluence_section}"
            "请像一线合约交易台的风控分析师在给同事讲盘一样输出，Telegram/网页纯文本，不用 Markdown 标记：\n"
            "- 第一行用一句话给出核心判断：这波更像什么（延续、反抽、突破确认、假突破、踩踏、分歧等待等），"
            "方向倾向和把握程度，必须结合后效统计和多周期共振来定调。\n"
            "- 随后写 3-5 条要点，围绕这组数据里最突出的驱动或矛盾来组织，小标题自拟，"
            "不要固定套用主假设/延续条件/失效条件/反向风险/观察计划这类模板标题，也不要每次都同一套结构。\n"
            "- 判断必须落到具体数字上：结构支撑/压力、VWAP、盘口墙、量能、OI、爆仓中至少引用三个，"
            "用来支撑推理，而不是机械复读字段。\n"
            "- 必须写清失效边界：哪个具体价位被破坏或哪类行为出现后，当前判断作废、应当认错。\n"
            "- 结尾说清接下来最该盯的 1-2 个信号（如二次放量、回踩是否缩量、OI 是否跟随、关键位争夺），"
            "不给绝对开仓指令。\n"
            f"{period_focus}"
            "如果样本可信度为 low 或样本不足，必须明确降低结论强度。"
            "如果爆仓数据状态不可用，不要把爆仓金额为0解读为没有强平压力。"
            "全文控制在 600 字以内，宁可少写一条也必须把失效边界和观察信号写完整，"
            "语言自然、有判断、有取舍，别像机器人逐条填表。"
        )

    @staticmethod
    def _question_intent_hint(question: str) -> str:
        text = str(question or "").lower()
        intent_rules = [
            (
                ("开空", "做空", "追空", "进空", "short", "急跌", "会跌", "下跌", "砸", "瀑布", "跳水"),
                "下行风险/做空可行性",
            ),
            (("追", "开多", "做多", "能进", "进多", "追涨", "long", "buy"), "追涨/做多可行性"),
            (("支撑", "压力", "关键位", "价位", "止损", "失效", "破位"), "关键价位/失效边界"),
            (("风险", "危险", "套", "爆仓", "强平", "插针"), "风险排查"),
            (("为什么", "原因", "触发", "异动"), "异动原因解释"),
            (("怎么看", "怎么样", "如何", "现在", "当前"), "当前状态概览"),
        ]
        for keywords, label in intent_rules:
            if any(keyword in text for keyword in keywords):
                return label
        return "综合判断"

    @staticmethod
    def _history_section(history: list[dict] | None) -> str:
        entries = [
            item
            for item in (history or [])
            if isinstance(item, dict) and str(item.get("question") or "").strip()
        ]
        if not entries:
            return ""
        lines = ["最近对话（按时间先后，用于衔接追问；结论仍以最新数据为准）："]
        for item in entries[-3:]:
            question = re.sub(r"\s+", " ", str(item.get("question") or "")).strip()
            answer = re.sub(r"\s+", " ", str(item.get("answer") or "")).strip()
            if len(question) > 120:
                question = f"{question[:119].rstrip()}..."
            if len(answer) > 240:
                answer = f"{answer[:239].rstrip()}..."
            symbol = str(item.get("symbol") or "").strip().upper()
            marker = f"[{symbol}] " if symbol else ""
            lines.append(f"- 用户{marker}问：{question}")
            if answer:
                lines.append(f"  你答：{answer}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _question_structure_section(timeframe_data: dict | None) -> str:
        if not timeframe_data:
            return ""
        period_label = timeframe_data.get("period_label") or timeframe_data.get("period") or "所选周期"
        candle_state = "已收线" if timeframe_data.get("candle_confirmed") else "进行中"
        return (
            f"\n用户关注的 {period_label} 周期结构数据（长周期问题以此为准）：\n"
            f"- K线状态: {candle_state}\n"
            f"- 周期开/高/低/收: {timeframe_data.get('open_price')} / {timeframe_data.get('high_price')} / "
            f"{timeframe_data.get('low_price')} / {timeframe_data.get('price')}\n"
            f"- 周期涨跌: {float(timeframe_data.get('price_move_pct', 0) or 0):+.3f}% | "
            f"量能倍数: {float(timeframe_data.get('volume_multiplier', 0) or 0):.2f}x\n"
            f"- 结构支撑: {timeframe_data.get('support_price')} "
            f"(强度={float(timeframe_data.get('support_strength') or 0):.2f}, "
            f"触碰={int(timeframe_data.get('support_touch_count') or 0)}, "
            f"状态={timeframe_data.get('support_status') or 'unknown'})\n"
            f"- 结构压力: {timeframe_data.get('resistance_price')} "
            f"(强度={float(timeframe_data.get('resistance_strength') or 0):.2f}, "
            f"触碰={int(timeframe_data.get('resistance_touch_count') or 0)}, "
            f"状态={timeframe_data.get('resistance_status') or 'unknown'})\n"
            f"- 结构状态: {timeframe_data.get('structure_regime') or 'unknown'} | "
            f"区间位置: {float(timeframe_data.get('range_position_pct', 0) or 0):.1f}%\n"
            f"- 成交密集POC: {timeframe_data.get('profile_poc_price')} | "
            f"价值区间: {timeframe_data.get('value_area_low')} ~ {timeframe_data.get('value_area_high')}\n"
            f"- 周期VWAP: {timeframe_data.get('window_vwap')} "
            f"(偏离 {float(timeframe_data.get('vwap_deviation_pct', 0) or 0):+.3f}%)\n"
            f"- 距支撑/距压力: {float(timeframe_data.get('support_distance_pct', 0) or 0):.3f}% / "
            f"{float(timeframe_data.get('resistance_distance_pct', 0) or 0):.3f}%\n"
            f"- 周期强平: 多头 {float(timeframe_data.get('period_long_liquidation_quote', 0) or 0):,.0f} / "
            f"空头 {float(timeframe_data.get('period_short_liquidation_quote', 0) or 0):,.0f} USDT\n"
        )

    def _build_question_prompt(
        self,
        question: str,
        data: dict | None,
        available_symbols: list[str],
        history: list[dict] | None = None,
        timeframe_data: dict | None = None,
        confluence_data: dict | None = None,
    ) -> str:
        intent_hint = self._question_intent_hint(question)
        history_section = self._history_section(history)
        if not data:
            symbols_text = ", ".join(available_symbols[:30]) or "暂无"
            return (
                f"用户通过 Telegram Bot 提问：{question}\n\n"
                f"用户意图初判（关键词粗判，仅供参考，以用户原话为准）：{intent_hint}\n"
                f"{history_section}"
                f"当前可查询合约：{symbols_text}\n"
                "请用中文自然简洁地回复，提醒用户带上明确合约，例如 BTC、ETH、SOL。"
                "如果用户已经写了合约但不在可查询列表里，请说明当前没有该合约数据。"
            )

        liquidation_status = {
            "recent_event": "近1分钟有强平事件",
            "no_recent_event": "强平数据已接入，近1分钟未捕获强平订单",
            "unavailable": "强平数据不可用",
        }.get(str(data.get("liquidation_data_status") or "unavailable"), "未知")
        reasons_text = "; ".join(data.get("reasons", [])) or "暂无"
        suggestions_text = "; ".join(data.get("suggestions", [])) or "暂无"
        has_structure_context = bool(timeframe_data or confluence_data)
        if has_structure_context:
            scope_text = (
                "当前可用数据范围：实时快照、近1m/5m波动、盘口/强平状态、历史后效统计，"
                "以及下方给出的周期结构和多周期共振数据；未提供的周期不要凭空下结论。\n\n"
            )
            structure_sections = (
                f"{self._question_structure_section(timeframe_data)}"
                f"{self._confluence_section(confluence_data)}"
            )
        else:
            scope_text = (
                "当前可用数据范围：实时快照、近1m/5m波动、盘口/强平状态和历史后效统计；"
                "没有更长周期K线细节时，请明确说明不能凭空确认长周期结构。\n\n"
            )
            structure_sections = ""
        return (
            f"用户通过 Telegram Bot 针对 {data.get('symbol')} 提问：{question}\n\n"
            f"用户意图初判（关键词粗判，仅供参考，以用户原话为准）：{intent_hint}\n"
            f"{history_section}"
            f"{scope_text}"
            "以下是该 USDT 永续合约的当前监控快照：\n"
            f"- 当前价格: {data.get('price')}\n"
            f"- 异常分: {data.get('score', 0)}/100\n"
            f"- 风险等级: {data.get('risk_level', '低风险')}\n"
            f"- 当前倾向: {data.get('bias', '')}\n"
            f"- 置信度: {float(data.get('confidence', 0)):.1f}/100\n"
            f"- 1分钟波动: {float(data.get('price_move_pct_1m', 0)):+.3f}%\n"
            f"- 5分钟波动: {float(data.get('price_move_pct_5m', 0)):+.3f}%\n"
            f"- 1分钟成交额: {float(data.get('quote_volume_1m', 0)):,.0f} USDT\n"
            f"- 量能倍数: {float(data.get('volume_multiplier', 0)):.2f}x\n"
            f"- 主动买入占比: {float(data.get('taker_buy_ratio_1m', 0.5)):.1%}\n"
            f"- OI 5分钟变化: {float(data.get('oi_change_pct_5m', 0)):+.3f}%\n"
            f"- 资金费率: {float(data.get('funding_rate', 0)):.4%}\n"
            f"- 实时标记价: {data.get('mark_price') or '--'}\n"
            f"- 实时标记价偏离: {float(data.get('mark_premium_bps', 0)):+.2f} bps\n"
            f"- 爆仓状态: {liquidation_status}\n"
            f"- 多头爆仓1m: {float(data.get('long_liquidation_quote_1m', 0)):,.0f} USDT\n"
            f"- 空头爆仓1m: {float(data.get('short_liquidation_quote_1m', 0)):,.0f} USDT\n"
            f"- 点差: {float(data.get('spread_bps', 0)):.2f} bps\n"
            f"- 盘口失衡: {float(data.get('depth_imbalance', 0)):+.3f}\n"
            f"- 盘口深度下降: {float(data.get('depth_drop_pct_1m', 0)):.1f}%\n"
            f"- 区间支撑 / 压力: {data.get('support_price')} / {data.get('resistance_price')}\n"
            f"- 距支撑 / 距压力: {float(data.get('support_distance_pct', 0)):.3f}% / {float(data.get('resistance_distance_pct', 0)):.3f}%\n"
            f"- 区间VWAP: {data.get('window_vwap')}\n"
            f"- 偏离VWAP: {float(data.get('vwap_deviation_pct', 0)):+.3f}%\n"
            f"- 区间位置: {float(data.get('range_position_pct', 50)):.1f}%\n"
            f"- 买盘墙 / 卖盘墙: {data.get('bid_wall_price')} / {data.get('ask_wall_price')}\n"
            f"- 买盘墙金额 / 卖盘墙金额: {float(data.get('bid_wall_notional', 0)):,.0f} / {float(data.get('ask_wall_notional', 0)):,.0f} USDT\n"
            f"- 触发原因: {reasons_text}\n"
            f"- 系统观察建议: {suggestions_text}\n"
            f"{self._stats_section(data)}"
            f"{structure_sections}\n"
            "回复方式：\n"
            "- 先用一句话直接回应用户真正问的点，语气可以自然，但结论必须有条件。\n"
            "- 接着只展开与问题相关的证据，2-5 个短段或要点即可；标题可以自由命名，不要固定套用主假设/确认条件/失效条件/反向风险。\n"
            "- 至少引用两个关键数据，例如价格、异常分、1m/5m波动、成交额、量能倍数、OI、VWAP、支撑压力、爆仓或盘口。\n"
            "- 如果用户问能不能追、做多或做空，要给出参与前确认、失效位置或撤退条件，但不能给绝对买卖指令。\n"
            "- 如果用户问会不会急跌、反弹或风险点，优先围绕触发条件、支撑/压力、OI、主动成交、爆仓和盘口深度回答。\n"
            "- 如果提供了周期结构或多周期共振数据，长周期判断以这些数据为准，并优先引用结构支撑/压力。\n"
            "- 如果提供了最近对话且这是追问，请自然衔接你上一轮的结论；观点变化时点明是哪个数据变了，不要整段重复旧回答。\n"
            "- 如果后效样本可信度为 low、样本不足或关键数据不可用，要降低结论强度；爆仓数据不可用时，不能把0爆仓解读为没有强平压力。\n"
            "- 用 Telegram 纯文本回复：不要使用 Markdown 标记（如 **、#、表格、代码块），列表用短横线即可。\n"
            "- 避免机械复读字段，像专业交易风控助理在 Telegram 里对话一样回答。"
        )

    def _system_prompt(self) -> str:
        return (
            "你是一名加密货币合约交易风险分析专家，精通USDT永续合约、"
            "杠杆交易策略、资金费率套利、持仓量分析、盘口微观结构解读、"
            "以及多空博弈下的风险评估。你的分析像资深交易员讲盘：直击要害、"
            "有明确观点和失效边界，从数据推出结论而不是罗列数据，绝不套模板凑字数。"
        )

    def _question_system_prompt(self) -> str:
        return (
            "你是一名加密货币合约交易风控分析师，正在 Telegram 中和用户多轮对话。"
            "你会先听懂用户问的是追多、做空、急跌风险、关键价位还是异动原因，"
            "再用给定行情数据作答；遇到追问时衔接自己上一轮的结论，观点变了要说明是哪个数据变了。"
            "表达要自然、有判断、有边界，只输出 Telegram 纯文本，"
            "不能编造未提供的数据，也不能给绝对收益承诺或无条件买卖指令。"
        )

    def _call_openai(
        self,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str | None:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
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
            if self._is_truncated(data):
                logging.warning(
                    "AI response truncated at max_tokens=%d (model=%s)", max_tokens, self.model
                )
                text = f"{text.rstrip()}\n（内容已达模型输出上限被截断）"
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

    @staticmethod
    def _is_truncated(data: dict) -> bool:
        reasons: set[str] = set()

        def collect(value) -> None:
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if not isinstance(value, dict):
                return
            for key in ("stop_reason", "finish_reason"):
                reason = value.get(key)
                if isinstance(reason, str):
                    reasons.add(reason)
            choices = value.get("choices")
            if isinstance(choices, list):
                collect(choices)

        collect(data)
        return bool(reasons & {"length", "max_tokens"})

    def _call_anthropic(
        self,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str | None:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
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
            if self._is_truncated(data):
                logging.warning(
                    "AI response truncated at max_tokens=%d (model=%s)", max_tokens, self.model
                )
                text = f"{text.rstrip()}\n（内容已达模型输出上限被截断）"
            return text
