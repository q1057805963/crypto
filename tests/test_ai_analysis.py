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
            "window_vwap": 61880,
            "vwap_deviation_pct": 0.19,
            "support_distance_pct": 1.31,
            "resistance_distance_pct": 0.81,
            "range_position_pct": 73.4,
            "mark_move_pct": 0.72,
            "mark_premium_bps": -3.5,
        }

        prompt = analyzer._build_prompt(snapshot, timeframe, "15m")

        self.assertIn("15m", prompt)
        self.assertIn("12,345,000 USDT", prompt)
        self.assertIn("1.67x", prompt)
        self.assertIn("-3.50 bps", prompt)

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
