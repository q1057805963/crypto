import asyncio
import unittest
from time import time

from monitor.ai_analysis import AIAnalyzer


class AIAnalyzerTests(unittest.TestCase):
    def test_cache_is_scoped_by_symbol_and_period(self) -> None:
        analyzer = AIAnalyzer({})
        analyzer._cache[analyzer._cache_key("BTCUSDT", "5m")] = (time(), "five minute view", None)

        self.assertEqual(analyzer.get_cached("BTCUSDT", "5m"), "five minute view")
        self.assertIsNone(analyzer.get_cached("BTCUSDT", "1h"))
        self.assertIsNone(analyzer.get_cached("ETHUSDT", "5m"))

    def test_get_cached_rejects_entry_when_snapshot_data_differs(self) -> None:
        analyzer = AIAnalyzer({})
        snapshot = {"symbol": "DOGEUSDT", "price": 0.07242, "score": 9.2, "bias": "偏空"}
        fingerprint = analyzer.snapshot_fingerprint(snapshot, period="alert")
        self.assertIsNotNone(fingerprint)
        analyzer._cache[analyzer._cache_key("DOGEUSDT", "alert")] = (
            time(),
            "stale stampede view",
            fingerprint,
        )

        self.assertEqual(
            analyzer.get_cached("DOGEUSDT", "alert", snapshot_data=snapshot),
            "stale stampede view",
        )
        changed = dict(snapshot, price=0.07262, score=0.0, bias="观察")
        self.assertIsNone(analyzer.get_cached("DOGEUSDT", "alert", snapshot_data=changed))
        # 不带数据时保持旧行为：只按 TTL 返回最近一次分析（仅用于展示）
        self.assertEqual(analyzer.get_cached("DOGEUSDT", "alert"), "stale stampede view")

    def test_analyze_regenerates_when_snapshot_changes_within_ttl(self) -> None:
        analyzer = AIAnalyzer(
            {
                "enabled": True,
                "api_key": "secret",
                "activation_threshold": 1,
                "retry_cooldown_seconds": 0,
            }
        )
        calls: list[str] = []

        def fake_call_prompt(prompt: str, **kwargs) -> str:
            calls.append(prompt)
            return f"analysis #{len(calls)}"

        analyzer._call_prompt = fake_call_prompt
        snapshot = {"symbol": "DOGEUSDT", "price": 0.07242, "score": 9.2, "bias": "偏空"}
        changed = dict(snapshot, price=0.07262, score=5.0, bias="观察")

        first = asyncio.run(analyzer.analyze("DOGEUSDT", snapshot, period="alert"))
        repeated = asyncio.run(analyzer.analyze("DOGEUSDT", snapshot, period="alert"))
        refreshed = asyncio.run(analyzer.analyze("DOGEUSDT", changed, period="alert"))

        self.assertEqual(first, "analysis #1")
        self.assertEqual(repeated, "analysis #1")
        self.assertEqual(refreshed, "analysis #2")
        self.assertEqual(len(calls), 2)

    def test_trigger_status_reports_score_threshold_match(self) -> None:
        analyzer = AIAnalyzer(
            {
                "activation_threshold": 30,
                "triggers": {
                    "mode": "any",
                    "conditions": {
                        "score": {"enabled": True, "threshold": 30},
                        "liquidation_total_quote_1m": {"enabled": True, "threshold": 10000},
                    },
                },
            }
        )

        status = analyzer.trigger_status({"score": 30.3, "liquidation_total_quote_1m": 0})

        self.assertTrue(status["matched"])
        self.assertEqual(status["mode"], "any")
        self.assertTrue(status["checks"][0]["matched"])
        self.assertFalse(status["checks"][1]["matched"])

    def test_build_prompt_includes_selected_timeframe_context(self) -> None:
        analyzer = AIAnalyzer({})
        snapshot = {
            "symbol": "BTCUSDT",
            "score": 82,
            "risk_level": "中风险",
            "bias": "偏多",
            "price": 62000,
            "price_move_pct_1m": 0.4,
            "price_move_pct_5m": 1.2,
            "quote_volume_1m": 1200000,
            "volume_multiplier": 1.8,
            "oi_change_pct_5m": 0.5,
            "funding_rate": 0.0001,
            "microstructure_status": "active",
            "liquidation_data_status": "recent_event",
            "liquidation_event_count_1m": 3,
            "long_liquidation_quote_1m": 200000,
            "short_liquidation_quote_1m": 100000,
            "spread_bps": 1.8,
            "depth_drop_pct_1m": 6.0,
            "support_price": 61200,
            "resistance_price": 62500,
            "window_vwap": 61880,
            "vwap_deviation_pct": 0.19,
            "bid_wall_price": 61750,
            "bid_wall_notional": 800000,
            "ask_wall_price": 62120,
            "ask_wall_notional": 760000,
            "reasons": ["volume expansion"],
            "trigger_combo": {"label": "放量 + 价仓同向", "key": "volume+oi_aligned"},
            "signal_stats": {
                "label": "15m",
                "sample_count": 12,
                "win_rate": 58.3,
                "avg_close_bps": 24.5,
                "avg_favorable_bps": 72.0,
                "avg_adverse_bps": 31.0,
                "reliability": "medium",
            },
            "combo_stats": {
                "label": "15m",
                "sample_count": 8,
                "win_rate": 62.5,
                "avg_close_bps": 31.5,
                "avg_favorable_bps": 81.0,
                "avg_adverse_bps": 28.0,
                "reliability": "low",
            },
        }
        timeframe = {
            "period_label": "15m",
            "candle_confirmed": False,
            "open_price": 61500,
            "high_price": 62280,
            "low_price": 61320,
            "price": 62000,
            "price_move_pct": 0.81,
            "prev_close_pct": 0.45,
            "quote_volume": 12345000,
            "volume_multiplier": 1.67,
            "support_price": 61200,
            "resistance_price": 62500,
            "period_low_price": 60400,
            "period_high_price": 63100,
            "support_source": "swing_cluster",
            "resistance_source": "touch_cluster",
            "support_touch_count": 4,
            "resistance_touch_count": 3,
            "support_pivot_count": 2,
            "resistance_pivot_count": 1,
            "support_strength": 12.4,
            "resistance_strength": 10.2,
            "structure_sample_count": 96,
            "structure_tolerance_pct": 0.35,
            "window_vwap": 61880,
            "vwap_deviation_pct": 0.19,
            "support_distance_pct": 1.31,
            "resistance_distance_pct": 0.81,
            "range_position_pct": 73.4,
            "mark_move_pct": 0.72,
            "mark_premium_bps": -3.5,
        }
        confluence = {
            "label": "多周期偏多共振",
            "direction": "up",
            "score": 72.5,
            "summary": "多周期偏多共振，3/4 个周期同向。",
            "confirmations": ["15m 上半区接受"],
            "conflicts": ["4h 测试压力"],
            "periods": [
                {
                    "period_label": "15m",
                    "structure_label": "上半区接受",
                    "bias": "up",
                    "price_move_pct": 0.8,
                    "volume_multiplier": 1.5,
                    "vwap_deviation_pct": 0.2,
                }
            ],
        }

        prompt = analyzer._build_prompt(snapshot, timeframe, "15m", confluence)

        self.assertIn("15m", prompt)
        self.assertIn("12,345,000 USDT", prompt)
        self.assertIn("1.67x", prompt)
        self.assertIn("-3.50 bps", prompt)
        self.assertIn("结构支撑: 61200", prompt)
        self.assertIn("来源=swing_cluster", prompt)
        self.assertIn("阶段最低/最高: 60400 / 63100", prompt)
        self.assertIn("96 根K线", prompt)
        self.assertIn("不要把旧的单根最高最低当作主支撑压力", prompt)
        self.assertIn("同组合后效", prompt)
        self.assertIn("多周期偏多共振", prompt)
        self.assertIn("核心判断", prompt)
        self.assertIn("失效边界", prompt)
        self.assertIn("不用编号、小标题和列表符号", prompt)

    def test_build_prompt_uses_alert_style_for_alert_period(self) -> None:
        analyzer = AIAnalyzer({})
        snapshot = {
            "symbol": "HYPEUSDT",
            "score": 60,
            "risk_level": "中风险",
            "bias": "偏空：疑似多头踩踏",
            "price": 70.031,
            "price_move_pct_1m": -0.638,
            "price_move_pct_5m": -1.057,
            "quote_volume_1m": 1818320,
            "volume_multiplier": 4.4,
            "taker_buy_ratio_1m": 0.12,
            "oi_change_pct_5m": -1.061,
            "funding_rate": 0.000087,
            "liquidation_data_status": "recent_event",
            "long_liquidation_quote_1m": 21912,
            "short_liquidation_quote_1m": 0,
            "spread_bps": 0.14,
            "depth_drop_pct_1m": 0.0,
            "support_price": 70.03,
            "resistance_price": 70.789,
            "window_vwap": 70.4,
            "vwap_deviation_pct": -0.5,
            "bid_wall_price": 70.167,
            "bid_wall_notional": 500000,
            "ask_wall_price": 70.18,
            "ask_wall_notional": 600000,
            "reasons": ["1分钟价格波动 -0.64%", "成交额放大 4.4x"],
        }

        prompt = analyzer._build_prompt(snapshot, period="alert")

        self.assertIn("异动告警", prompt)
        self.assertIn("观察建议", prompt)
        self.assertIn("失效/反转确认条件", prompt)
        self.assertIn("不要写放之四海而皆准的套话", prompt)
        self.assertNotIn("用户当前更关心", prompt)

    def test_extract_text_supports_anthropic_style_content(self) -> None:
        payload = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "第一条观察"},
                {"type": "text", "text": "第二条观察"},
            ],
            "stop_reason": "end_turn",
        }

        self.assertEqual(
            AIAnalyzer._extract_text(payload),
            "第一条观察\n第二条观察",
        )

    def test_build_question_prompt_is_intent_aware_and_conversational(self) -> None:
        analyzer = AIAnalyzer({})
        snapshot = {
            "symbol": "BTCUSDT",
            "price": 62000,
            "score": 68,
            "risk_level": "中风险",
            "bias": "偏多：价格主动上行",
            "confidence": 71,
            "price_move_pct_1m": 0.72,
            "price_move_pct_5m": 1.65,
            "quote_volume_1m": 1800000,
            "volume_multiplier": 2.7,
            "taker_buy_ratio_1m": 0.69,
            "oi_change_pct_5m": 1.1,
            "funding_rate": 0.0002,
            "mark_price": 62010,
            "mark_premium_bps": 1.6,
            "liquidation_data_status": "no_recent_event",
            "long_liquidation_quote_1m": 0,
            "short_liquidation_quote_1m": 42000,
            "spread_bps": 1.2,
            "depth_imbalance": 0.18,
            "depth_drop_pct_1m": 4.0,
            "support_price": 61200,
            "resistance_price": 62500,
            "support_distance_pct": 1.29,
            "resistance_distance_pct": 0.81,
            "window_vwap": 61780,
            "vwap_deviation_pct": 0.36,
            "range_position_pct": 76.0,
            "bid_wall_price": 61850,
            "bid_wall_notional": 780000,
            "ask_wall_price": 62480,
            "ask_wall_notional": 650000,
            "reasons": ["1分钟价格波动 +0.72%", "成交额放大 2.7x"],
            "suggestions": ["等待回踩不破 VWAP 再确认"],
        }

        prompt = analyzer._build_question_prompt("BTC 现在能追多吗？", snapshot, ["BTCUSDT"])

        self.assertIn("用户意图初判（关键词粗判，仅供参考，以用户原话为准）：追涨/做多可行性", prompt)
        self.assertIn("当前可用数据范围：实时快照、近1m/5m波动", prompt)
        self.assertIn("至少引用两个关键数据", prompt)
        self.assertIn("不要固定套用主假设/确认条件/失效条件/反向风险", prompt)
        self.assertIn("系统观察建议: 等待回踩不破 VWAP 再确认", prompt)
        self.assertIn("距支撑 / 距压力: 1.290% / 0.810%", prompt)
        self.assertIn("用 Telegram 纯文本回复", prompt)

    def test_question_intent_hint_prefers_short_side_keywords(self) -> None:
        self.assertEqual(
            AIAnalyzer._question_intent_hint("还能进空吗？"),
            "下行风险/做空可行性",
        )
        self.assertEqual(
            AIAnalyzer._question_intent_hint("现在追空还是等反弹？"),
            "下行风险/做空可行性",
        )
        self.assertEqual(
            AIAnalyzer._question_intent_hint("BTC 现在能追多吗？"),
            "追涨/做多可行性",
        )

    def test_build_question_prompt_includes_history_and_structure_context(self) -> None:
        analyzer = AIAnalyzer({})
        snapshot = {
            "symbol": "BTCUSDT",
            "price": 62000,
            "score": 55,
            "reasons": [],
            "suggestions": [],
        }
        history = [
            {
                "question": "BTC 现在能追多吗？",
                "answer": "先别急着追，量能还没跟上，回踩 61800 不破再看。",
                "symbol": "BTCUSDT",
            }
        ]
        timeframe_data = {
            "period_label": "1小时",
            "candle_confirmed": True,
            "open_price": 61500,
            "high_price": 62400,
            "low_price": 61300,
            "price": 62000,
            "price_move_pct": 0.81,
            "volume_multiplier": 1.4,
            "support_price": 61250,
            "support_strength": 0.8,
            "support_touch_count": 3,
            "support_status": "holding",
            "resistance_price": 62600,
            "resistance_strength": 0.7,
            "resistance_touch_count": 2,
            "resistance_status": "capping",
            "structure_regime": "range",
            "range_position_pct": 62.0,
            "profile_poc_price": 61800,
            "value_area_low": 61500,
            "value_area_high": 62300,
            "window_vwap": 61850,
            "vwap_deviation_pct": 0.24,
            "support_distance_pct": 1.21,
            "resistance_distance_pct": 0.97,
            "period_long_liquidation_quote": 120000,
            "period_short_liquidation_quote": 340000,
        }
        confluence_data = {
            "label": "多头共振",
            "direction": "up",
            "score": 66.0,
            "summary": "1h/4h 同向",
            "confirmations": ["1h 站上 VWAP"],
            "conflicts": [],
            "periods": [],
        }

        prompt = analyzer._build_question_prompt(
            "那 1 小时的支撑在哪里？",
            snapshot,
            ["BTCUSDT"],
            history=history,
            timeframe_data=timeframe_data,
            confluence_data=confluence_data,
        )

        self.assertIn("最近对话（按时间先后", prompt)
        self.assertIn("用户[BTCUSDT] 问：BTC 现在能追多吗？", prompt)
        self.assertIn("你答：先别急着追", prompt)
        self.assertIn("用户关注的 1小时 周期结构数据", prompt)
        self.assertIn("结构支撑: 61250", prompt)
        self.assertIn("多周期共振", prompt)
        self.assertIn("未提供的周期不要凭空下结论", prompt)
        self.assertIn("请自然衔接你上一轮的结论", prompt)
        self.assertNotIn("不能凭空确认长周期结构", prompt)

    def test_build_question_prompt_without_structure_keeps_scope_guardrail(self) -> None:
        analyzer = AIAnalyzer({})
        prompt = analyzer._build_question_prompt(
            "BTC 4小时结构怎么样？",
            {"symbol": "BTCUSDT", "price": 62000, "reasons": [], "suggestions": []},
            ["BTCUSDT"],
        )

        self.assertIn("不能凭空确认长周期结构", prompt)
        self.assertNotIn("最近对话（按时间先后", prompt)

    def test_question_api_uses_question_style_generation_settings(self) -> None:
        analyzer = AIAnalyzer(
            {
                "enabled": True,
                "api_key": "secret",
                "max_tokens": 500,
                "question_max_tokens": 760,
                "question_temperature": 0.62,
            }
        )
        calls = []

        def fake_call_prompt(prompt: str, **kwargs) -> str:
            calls.append({"prompt": prompt, **kwargs})
            return "ok"

        analyzer._call_prompt = fake_call_prompt

        result = analyzer._call_question_api("ETH 会不会急跌？", {"symbol": "ETHUSDT"}, ["ETHUSDT"])

        self.assertEqual(result, "ok")
        self.assertEqual(calls[0]["temperature"], 0.62)
        self.assertEqual(calls[0]["max_tokens"], 760)
        self.assertIn("Telegram", calls[0]["system_prompt"])
        self.assertIn(
            "用户意图初判（关键词粗判，仅供参考，以用户原话为准）：下行风险/做空可行性",
            calls[0]["prompt"],
        )


if __name__ == "__main__":
    unittest.main()
