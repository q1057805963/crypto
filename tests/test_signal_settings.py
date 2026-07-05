import unittest

from monitor.anomaly import AnomalyDetector
from monitor.dashboard import apply_signal_settings, signal_settings_response


class SignalSettingsTests(unittest.TestCase):
    def build_detector(self) -> AnomalyDetector:
        return AnomalyDetector(
            symbols=["BTCUSDT"],
            window_seconds=300,
            warmup_seconds=0,
            alert_cooldown_seconds=0,
            thresholds={
                "price_move_pct_1m": 0.6,
                "price_move_pct_5m": 1.2,
                "volume_multiplier": 2.2,
                "taker_buy_ratio_high": 0.68,
                "taker_buy_ratio_low": 0.32,
                "min_quote_volume_1m": 300000,
                "anomaly_score": 60,
                "oi_change_pct_5m": 0.8,
                "funding_rate_abs": 0.0003,
                "liquidation_enabled": True,
                "liquidation_quote_1m": 75000,
                "spread_enabled": True,
                "spread_bps": 3.0,
                "depth_imbalance_enabled": True,
                "depth_imbalance_abs": 0.22,
                "depth_drop_enabled": True,
                "depth_drop_pct_1m": 15.0,
            },
        )

    @staticmethod
    def trade(
        event_time: float,
        *,
        symbol: str = "BTCUSDT",
        price: float = 100.0,
        quantity: float = 1.0,
        quote_quantity: float = 100.0,
        side: str = "buy",
    ) -> dict:
        return {
            "symbol": symbol,
            "event_time": event_time,
            "price": price,
            "quantity": quantity,
            "quote_quantity": quote_quantity,
            "side": side,
        }

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

    def test_cold_start_volume_multiplier_uses_observed_window(self) -> None:
        detector = self.build_detector()

        detector.update(self.trade(1000.0, quote_quantity=100.0))
        snapshot = detector.snapshot("BTCUSDT")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.volume_multiplier, 1.0)
        self.assertEqual(snapshot.score, 0.0)
        self.assertIn("主动成交样本不足，方向占比降权", snapshot.reasons)

    def test_taker_ratio_requires_enough_realtime_samples(self) -> None:
        detector = self.build_detector()

        score, reasons = detector._score(
            price_move_pct_1m=0,
            price_move_pct_5m=0,
            quote_volume_1m=1000000,
            volume_multiplier=1,
            taker_buy_ratio=1.0,
            oi_change_pct_5m=0,
            funding_rate=0,
            spread_bps=0,
            depth_imbalance=0,
            depth_drop_pct_1m=0,
            long_liquidation_quote_1m=0,
            short_liquidation_quote_1m=0,
            trade_count_1m=2,
        )

        self.assertEqual(score, 0)
        self.assertIn("主动成交样本不足，方向占比降权", reasons)

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

    def test_professional_scoring_requires_confluence_for_alert_level(self) -> None:
        detector = self.build_detector()

        price_only_score, price_only_reasons = detector._score(
            price_move_pct_1m=3.0,
            price_move_pct_5m=4.0,
            quote_volume_1m=1000000,
            volume_multiplier=1.0,
            taker_buy_ratio=0.5,
            oi_change_pct_5m=0,
            funding_rate=0,
            spread_bps=0,
            depth_imbalance=0,
            depth_drop_pct_1m=0,
            long_liquidation_quote_1m=0,
            short_liquidation_quote_1m=0,
        )

        self.assertLess(price_only_score, 60)
        self.assertIn("1分钟价格波动 +3.00%", price_only_reasons)

        confluence_score, confluence_reasons = detector._score(
            price_move_pct_1m=1.2,
            price_move_pct_5m=2.4,
            quote_volume_1m=1200000,
            volume_multiplier=3.6,
            taker_buy_ratio=0.74,
            oi_change_pct_5m=2.1,
            funding_rate=0.0004,
            spread_bps=1.2,
            depth_imbalance=0.15,
            depth_drop_pct_1m=6,
            long_liquidation_quote_1m=0,
            short_liquidation_quote_1m=50000,
        )

        self.assertGreaterEqual(confluence_score, 60)
        self.assertIn("价格与放量共振", confluence_reasons)
        self.assertIn("主动成交与价格方向一致", confluence_reasons)
        self.assertIn("价格与持仓同向增加", confluence_reasons)


if __name__ == "__main__":
    unittest.main()
