import json
import unittest
from unittest.mock import patch

from monitor.timeframe_analysis import (
    TimeframeAnalysisService,
    build_followup_result,
    build_multi_timeframe_confluence,
    build_timeframe_analysis,
)


class TimeframeAnalysisTests(unittest.TestCase):
    def test_okx_current_timeframe_uses_latest_candle_endpoints(self) -> None:
        requested_urls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "code": "0",
                        "data": [
                            [
                                "1710000000000",
                                "620",
                                "650",
                                "610",
                                "640",
                                "1",
                                "10",
                                "6400",
                                "0",
                            ]
                        ],
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            requested_urls.append(request.full_url)
            return FakeResponse()

        service = TimeframeAnalysisService()
        with patch("monitor.timeframe_analysis.urlopen", fake_urlopen):
            price_candles = service._fetch_okx_candles("BNBUSDT", "4H", mark=False)
            service._fetch_okx_candles("BNBUSDT", "4H", mark=True)

        self.assertIn("/api/v5/market/candles?", requested_urls[0])
        self.assertIn("/api/v5/market/mark-price-candles?", requested_urls[1])
        self.assertAlmostEqual(price_candles[0]["close_time"] - price_candles[0]["open_time"], 14400)

    def test_build_timeframe_analysis_from_exchange_candles(self) -> None:
        price_candles = [
            {
                "open_time": 1000,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "base_volume": 10.0,
                "quote_volume": 1010.0,
                "confirmed": True,
            },
            {
                "open_time": 1300,
                "open": 101.0,
                "high": 106.0,
                "low": 100.0,
                "close": 105.0,
                "base_volume": 12.0,
                "quote_volume": 1260.0,
                "confirmed": True,
            },
            {
                "open_time": 1600,
                "open": 105.0,
                "high": 108.0,
                "low": 103.0,
                "close": 107.0,
                "base_volume": 15.0,
                "quote_volume": 1605.0,
                "confirmed": False,
            },
        ]
        mark_candles = [
            {
                "open_time": 1000,
                "open": 100.1,
                "high": 101.8,
                "low": 99.2,
                "close": 100.9,
                "base_volume": 0.0,
                "quote_volume": 0.0,
                "confirmed": True,
            },
            {
                "open_time": 1300,
                "open": 101.1,
                "high": 105.8,
                "low": 100.2,
                "close": 104.7,
                "base_volume": 0.0,
                "quote_volume": 0.0,
                "confirmed": True,
            },
            {
                "open_time": 1600,
                "open": 105.2,
                "high": 107.5,
                "low": 104.1,
                "close": 106.6,
                "base_volume": 0.0,
                "quote_volume": 0.0,
                "confirmed": False,
            },
        ]

        result = build_timeframe_analysis(
            symbol="BTCUSDT",
            period="5m",
            exchange="binance_usdm",
            price_candles=price_candles,
            mark_candles=mark_candles,
        )

        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["period"], "5m")
        self.assertAlmostEqual(result["price"], 107.0)
        self.assertAlmostEqual(result["price_move_pct"], 1.905, places=3)
        self.assertAlmostEqual(result["prev_close_pct"], 1.905, places=3)
        self.assertAlmostEqual(result["support_price"], 99.0)
        self.assertAlmostEqual(result["resistance_price"], 108.0)
        self.assertAlmostEqual(result["window_vwap"], 104.72973, places=4)
        self.assertAlmostEqual(result["vwap_deviation_pct"], 2.168, places=3)
        self.assertAlmostEqual(result["range_position_pct"], 88.89, places=2)
        self.assertAlmostEqual(result["volume_multiplier"], 1.41, places=2)
        self.assertEqual(result["low_series"], [99.0, 100.0, 103.0])
        self.assertEqual(result["high_series"], [102.0, 106.0, 108.0])
        self.assertIn(result["support_price"], result["low_series"])
        self.assertIn(result["resistance_price"], result["high_series"])
        self.assertEqual(result["period_low_price"], 99.0)
        self.assertEqual(result["period_high_price"], 108.0)
        self.assertEqual(result["support_source"], "range_low")
        self.assertEqual(result["resistance_source"], "range_high")
        self.assertAlmostEqual(result["mark_price"], 106.6)
        self.assertAlmostEqual(result["mark_premium_bps"], -37.383, places=3)
        self.assertEqual(len(result["price_series"]), 3)
        self.assertEqual(len(result["mark_price_series"]), 3)

    def test_timeframe_analysis_prefers_confirmed_structure_over_far_wick(self) -> None:
        lows = [104, 102, 103, 80, 102, 101.8, 102.2, 103, 102.1, 104, 103.8, 105]
        highs = [112, 114, 113, 111, 115, 114.8, 115.2, 114.9, 116, 115.1, 115.3, 113]
        price_candles = []
        for index, (low, high) in enumerate(zip(lows, highs)):
            close = 110 + (index % 3) * 0.4
            price_candles.append(
                {
                    "open_time": 1000 + index * 300,
                    "open": close - 0.3,
                    "high": high,
                    "low": low,
                    "close": 112.0 if index == len(lows) - 1 else close,
                    "base_volume": 10.0 + index,
                    "quote_volume": (10.0 + index) * close,
                    "confirmed": index < len(lows) - 1,
                }
            )

        result = build_timeframe_analysis(
            symbol="BNBUSDT",
            period="5m",
            exchange="okx_swap",
            price_candles=price_candles,
            mark_candles=[],
        )

        self.assertEqual(result["period_low_price"], 80.0)
        self.assertGreater(result["support_price"], 100.0)
        self.assertIn(result["support_source"], {"touch_cluster", "swing_cluster", "volume_profile_cluster", "swing_volume_cluster"})
        self.assertGreater(result["support_touch_count"], 1)
        self.assertGreater(result["resistance_touch_count"], 1)
        self.assertGreater(result["profile_poc_price"], 0)
        self.assertGreater(result["value_area_high"], result["value_area_low"])
        self.assertGreaterEqual(result["support_confluence_score"], 0)
        self.assertGreaterEqual(result["resistance_confluence_score"], 0)
        self.assertIn(
            result["structure_regime"],
            {
                "support_lost",
                "resistance_breakout",
                "support_test",
                "resistance_test",
                "value_area_rotation",
                "upper_acceptance",
                "lower_acceptance",
                "balanced",
            },
        )

    def test_build_followup_result_from_native_candles(self) -> None:
        result = build_followup_result(
            symbol="BTCUSDT",
            exchange="binance_usdm",
            horizon_minutes=5,
            event_time=1000.0,
            target_time=1300.0,
            anchor_price=100.0,
            interval_seconds=60,
            price_candles=[
                {"open_time": 960.0, "close_time": 1019.0, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.5},
                {"open_time": 1020.0, "close_time": 1079.0, "open": 100.5, "high": 104.0, "low": 100.0, "close": 103.0},
                {"open_time": 1260.0, "close_time": 1319.0, "open": 103.0, "high": 105.0, "low": 102.0, "close": 104.0},
            ],
            mark_candles=[
                {"open_time": 960.0, "close_time": 1019.0, "open": 100.0, "high": 100.8, "low": 99.8, "close": 100.4},
                {"open_time": 1260.0, "close_time": 1319.0, "open": 100.4, "high": 104.2, "low": 100.1, "close": 103.8},
            ],
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["source"], "exchange_klines")
        self.assertAlmostEqual(result["close_bps"], 400.0)
        self.assertAlmostEqual(result["max_up_bps"], 500.0)
        self.assertAlmostEqual(result["max_down_bps"], -200.0)
        self.assertEqual(result["sample_count"], 3)
        self.assertAlmostEqual(result["mark_close_bps"], 380.0)

    def test_build_multi_timeframe_confluence_summarizes_alignment_and_conflicts(self) -> None:
        analyses = [
            {
                "period": "5m",
                "period_label": "5m",
                "structure_regime": "resistance_breakout",
                "price_move_pct": 1.1,
                "volume_multiplier": 1.8,
                "vwap_deviation_pct": 0.4,
                "range_position_pct": 82,
            },
            {
                "period": "15m",
                "period_label": "15m",
                "structure_regime": "upper_acceptance",
                "price_move_pct": 0.7,
                "volume_multiplier": 1.3,
                "vwap_deviation_pct": 0.2,
                "range_position_pct": 76,
            },
            {
                "period": "1h",
                "period_label": "1h",
                "structure_regime": "upper_acceptance",
                "price_move_pct": 0.9,
                "volume_multiplier": 1.6,
                "vwap_deviation_pct": 0.3,
                "range_position_pct": 72,
            },
            {
                "period": "4h",
                "period_label": "4h",
                "structure_regime": "resistance_test",
                "price_move_pct": -0.4,
                "volume_multiplier": 1.1,
                "vwap_deviation_pct": -0.1,
                "range_position_pct": 68,
            },
        ]

        result = build_multi_timeframe_confluence(
            symbol="BTCUSDT",
            exchange="okx_swap",
            analyses=analyses,
        )

        self.assertEqual(result["direction"], "up")
        self.assertGreater(result["score"], 40)
        self.assertEqual(result["sample_count"], 4)
        self.assertTrue(result["confirmations"])
        self.assertTrue(result["conflicts"])


if __name__ == "__main__":
    unittest.main()
