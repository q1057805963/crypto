import unittest
from time import time

from monitor.ai_analysis import AIAnalyzer


class AIAnalyzerTests(unittest.TestCase):
    def test_cache_is_scoped_by_symbol_and_period(self) -> None:
        analyzer = AIAnalyzer({})
        analyzer._cache[analyzer._cache_key("BTCUSDT", "5m")] = (time(), "five minute view")

        self.assertEqual(analyzer.get_cached("BTCUSDT", "5m"), "five minute view")
        self.assertIsNone(analyzer.get_cached("BTCUSDT", "1h"))
        self.assertIsNone(analyzer.get_cached("ETHUSDT", "5m"))

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
        self.assertIn("主假设", prompt)
        self.assertIn("延续条件", prompt)
        self.assertIn("失效条件", prompt)

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


if __name__ == "__main__":
    unittest.main()
