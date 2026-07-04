import unittest

from monitor.microstructure import MarketMicrostructureState


class MicrostructureLiquidationTests(unittest.TestCase):
    def test_period_liquidation_summary_uses_selected_window_without_changing_1m_snapshot(self) -> None:
        state = MarketMicrostructureState(
            ["BTCUSDT"],
            liquidation_feed_mode="stream",
            liquidation_retention_seconds=3600,
        )
        state.record_depth(
            "BTCUSDT",
            event_time=1000.0,
            bids=[["99", "10"]],
            asks=[["101", "10"]],
        )
        state.record_liquidation("BTCUSDT", 600.0, "SELL", 100.0, 1.0)
        state.record_liquidation("BTCUSDT", 760.0, "SELL", 100.0, 2.0)
        state.record_liquidation("BTCUSDT", 980.0, "BUY", 110.0, 3.0)

        period = state.liquidation_summary("BTCUSDT", seconds=300, now=1000.0)
        realtime = state.snapshot("BTCUSDT", now=1000.0)

        self.assertEqual(period["period_liquidation_data_status"], "recent_event")
        self.assertEqual(period["period_liquidation_event_count"], 2)
        self.assertAlmostEqual(period["period_long_liquidation_quote"], 200.0)
        self.assertAlmostEqual(period["period_short_liquidation_quote"], 330.0)
        self.assertAlmostEqual(period["period_liquidation_total_quote"], 530.0)
        self.assertEqual(realtime["liquidation_event_count_1m"], 1)
        self.assertAlmostEqual(realtime["long_liquidation_quote_1m"], 0.0)
        self.assertAlmostEqual(realtime["short_liquidation_quote_1m"], 330.0)


if __name__ == "__main__":
    unittest.main()
