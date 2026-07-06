import unittest

from monitor.anomaly import AnomalyDetector


def make_detector(thresholds: dict | None = None) -> AnomalyDetector:
    return AnomalyDetector(
        symbols=["BTCUSDT"],
        window_seconds=300,
        warmup_seconds=0,
        alert_cooldown_seconds=0,
        thresholds=thresholds or {},
    )


def make_trade(event_time: float, quote: float, price: float = 100.0, side: str = "buy") -> dict:
    return {
        "symbol": "BTCUSDT",
        "event_time": event_time,
        "price": price,
        "quantity": quote / price,
        "quote_quantity": quote,
        "side": side,
    }


class AnomalyDetectorTests(unittest.TestCase):
    def test_volume_multiplier_baseline_excludes_current_minute(self) -> None:
        detector = make_detector()
        # 4分钟基线，每分钟 10k
        for index in range(4):
            detector.update(make_trade(100 + index * 60, 10_000))
        # 当前1分钟爆量 100k
        detector.update(make_trade(395, 50_000))
        detector.update(make_trade(398, 50_000))

        snapshot = detector.snapshot("BTCUSDT")

        self.assertIsNotNone(snapshot)
        # 旧算法把爆量算进基线，倍数被压到 ~3.5x；正确值应接近 10x
        self.assertGreater(snapshot.volume_multiplier, 8.0)
        self.assertLess(snapshot.volume_multiplier, 12.0)

    def test_volume_multiplier_neutral_without_baseline(self) -> None:
        detector = make_detector()
        detector.update(make_trade(395, 50_000))
        detector.update(make_trade(398, 50_000))

        snapshot = detector.snapshot("BTCUSDT")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.volume_multiplier, 1.0)

    def test_volume_multiplier_is_capped(self) -> None:
        detector = make_detector()
        for index in range(4):
            detector.update(make_trade(100 + index * 60, 10))
        detector.update(make_trade(398, 100_000))

        snapshot = detector.snapshot("BTCUSDT")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.volume_multiplier, 50.0)

    def test_component_score_has_floor_at_trigger(self) -> None:
        self.assertEqual(AnomalyDetector._component_score(0.5, 0.6, 1.5, 20), 0.0)
        self.assertAlmostEqual(AnomalyDetector._component_score(0.6, 0.6, 1.5, 20), 6.0)
        self.assertAlmostEqual(AnomalyDetector._component_score(1.5, 0.6, 1.5, 20), 20.0)
        # 中点应落在基础分与上限之间
        midpoint = AnomalyDetector._component_score(1.05, 0.6, 1.5, 20)
        self.assertGreater(midpoint, 6.0)
        self.assertLess(midpoint, 20.0)

    def test_bias_respects_configured_oi_threshold(self) -> None:
        self.assertEqual(
            AnomalyDetector._bias("up", 1.0, 1.0, 0, 0, 0, 0, 0, oi_threshold=2.0),
            "偏多：价格主动上行",
        )
        self.assertEqual(
            AnomalyDetector._bias("up", 1.0, 1.0, 0, 0, 0, 0, 0),
            "偏多：疑似新增资金推动",
        )

    def test_bias_pin_risk_uses_configured_depth_and_spread(self) -> None:
        self.assertEqual(
            AnomalyDetector._bias(
                "up", 0, 0, 0, 0, 0, 5.0, 20.0,
                spread_threshold=3.0, depth_drop_threshold=15.0,
            ),
            "插针风险：盘口明显变薄",
        )
        self.assertEqual(
            AnomalyDetector._bias(
                "up", 0, 0, 0, 0, 0, 5.0, 20.0,
                spread_threshold=8.0, depth_drop_threshold=30.0,
            ),
            "偏多：价格主动上行",
        )


if __name__ == "__main__":
    unittest.main()
