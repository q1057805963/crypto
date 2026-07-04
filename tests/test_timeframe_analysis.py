import unittest

from monitor.timeframe_analysis import build_followup_result, build_timeframe_analysis


class TimeframeAnalysisTests(unittest.TestCase):
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
        self.assertAlmostEqual(result["mark_price"], 106.6)
        self.assertAlmostEqual(result["mark_premium_bps"], -37.383, places=3)
        self.assertEqual(len(result["price_series"]), 3)
        self.assertEqual(len(result["mark_price_series"]), 3)

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


if __name__ == "__main__":
    unittest.main()
