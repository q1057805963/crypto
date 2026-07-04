import unittest

from monitor.anomaly import AnomalyDetector
from monitor.dashboard import apply_signal_settings, signal_settings_response


class SignalSettingsTests(unittest.TestCase):
    def test_signal_settings_response_and_apply_update_thresholds(self) -> None:
        thresholds = {
            "liquidation_enabled": True,
            "liquidation_quote_1m": 250000,
            "spread_enabled": True,
            "spread_bps": 4.0,
        }

        updated = apply_signal_settings(
            thresholds,
            {
                "liquidation": {"enabled": False, "threshold": 100000},
                "spread": {"enabled": True, "threshold": 2.5},
            },
        )

        self.assertFalse(updated["liquidation_enabled"])
        self.assertEqual(updated["liquidation_quote_1m"], 100000)
        self.assertTrue(updated["spread_enabled"])
        self.assertEqual(updated["spread_bps"], 2.5)
        response = signal_settings_response(updated)
        self.assertFalse(response["liquidation"]["enabled"])
        self.assertEqual(response["liquidation"]["threshold"], 100000)

    def test_disabled_microstructure_signals_do_not_score_or_add_reasons(self) -> None:
        detector = AnomalyDetector(
            symbols=["BTCUSDT"],
            window_seconds=300,
            warmup_seconds=0,
            alert_cooldown_seconds=0,
            thresholds={
                "price_move_pct_1m": 99,
                "price_move_pct_5m": 99,
                "volume_multiplier": 99,
                "taker_buy_ratio_high": 0.99,
                "taker_buy_ratio_low": 0.01,
                "oi_change_pct_5m": 99,
                "funding_rate_abs": 99,
                "liquidation_enabled": False,
                "liquidation_quote_1m": 1,
                "spread_enabled": False,
                "spread_bps": 1,
                "depth_imbalance_enabled": False,
                "depth_imbalance_abs": 0.01,
                "depth_drop_enabled": False,
                "depth_drop_pct_1m": 1,
            },
        )

        score, reasons = detector._score(
            price_move_pct_1m=0,
            price_move_pct_5m=0,
            quote_volume_1m=1000000,
            volume_multiplier=1,
            taker_buy_ratio=0.5,
            oi_change_pct_5m=0,
            funding_rate=0,
            spread_bps=10,
            depth_imbalance=0.5,
            depth_drop_pct_1m=50,
            long_liquidation_quote_1m=1000000,
            short_liquidation_quote_1m=0,
        )

        self.assertEqual(score, 0)
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
