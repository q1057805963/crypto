import tempfile
import unittest
from pathlib import Path

from monitor.anomaly import AnomalyEvent, SymbolSnapshot
from monitor.storage import AlertStore


def build_event(**overrides) -> AnomalyEvent:
    payload = {
        "symbol": "BTCUSDT",
        "score": 82.5,
        "direction": "up",
        "price": 100.0,
        "price_move_pct_1m": 1.2,
        "price_move_pct_5m": 2.4,
        "quote_volume_1m": 1200000.0,
        "volume_multiplier": 3.6,
        "taker_buy_ratio_1m": 0.74,
        "open_interest": 100000.0,
        "oi_change_pct_5m": 2.1,
        "funding_rate": 0.0004,
        "mark_price": 100.08,
        "mark_premium_bps": 8.0,
        "spread_bps": 1.2,
        "depth_imbalance": 0.15,
        "bid_depth_notional": 500000.0,
        "ask_depth_notional": 400000.0,
        "depth_drop_pct_1m": 6.0,
        "long_liquidation_quote_1m": 0.0,
        "short_liquidation_quote_1m": 50000.0,
        "liquidation_total_quote_1m": 50000.0,
        "risk_level": "高风险",
        "bias": "偏多：疑似新增资金推动",
        "confidence": 78.0,
        "reasons": ("价格拉升", "量能放大"),
        "suggestions": ("看回踩承接", "关注 OI 延续"),
        "event_time": 1000.0,
        "ai_analysis": "",
        "ai_summary": (),
    }
    payload.update(overrides)
    return AnomalyEvent(**payload)


def build_snapshot(updated_at: float, price: float, **overrides) -> SymbolSnapshot:
    payload = {
        "symbol": "BTCUSDT",
        "score": 60.0,
        "direction": "up",
        "price": price,
        "updated_at": updated_at,
        "price_move_pct_1m": 0.8,
        "price_move_pct_5m": 1.5,
        "quote_volume_1m": 800000.0,
        "volume_multiplier": 2.0,
        "taker_buy_ratio_1m": 0.6,
        "trade_count_1m": 20,
        "open_interest": 100000.0,
        "oi_change_pct_5m": 1.1,
        "funding_rate": 0.0003,
        "mark_price": price,
        "mark_premium_bps": 0.0,
        "spread_bps": 1.0,
        "depth_imbalance": 0.1,
        "bid_depth_notional": 400000.0,
        "ask_depth_notional": 350000.0,
        "depth_drop_pct_1m": 5.0,
        "support_price": 95.0,
        "resistance_price": 105.0,
        "support_distance_pct": 2.0,
        "resistance_distance_pct": 3.0,
        "window_vwap": 99.5,
        "vwap_deviation_pct": 0.5,
        "range_position_pct": 70.0,
        "bid_wall_price": 99.0,
        "bid_wall_notional": 120000.0,
        "ask_wall_price": 101.0,
        "ask_wall_notional": 110000.0,
        "long_liquidation_quote_1m": 0.0,
        "short_liquidation_quote_1m": 0.0,
        "liquidation_total_quote_1m": 0.0,
        "liquidation_event_count_1m": 0,
        "liquidation_data_status": "no_recent_event",
        "microstructure_status": "active",
        "depth_data_age_seconds": 1.0,
        "last_liquidation_age_seconds": None,
        "price_series_5m": (98.0, 99.0, 100.0, 101.0, price),
        "volume_series_5m": (200000.0, 220000.0, 250000.0, 300000.0, 320000.0),
        "oi_series_5m": (98000.0, 99000.0, 99500.0, 100000.0, 100200.0),
        "risk_level": "中风险",
        "bias": "偏多：价格主动上行",
        "confidence": 60.0,
        "reasons": ("价格抬升",),
        "suggestions": ("继续观察",),
    }
    payload.update(overrides)
    return SymbolSnapshot(**payload)


class AlertStoreFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.db"
        self.store = AlertStore(str(self.db_path), snapshot_interval_seconds=60)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_followups_start_pending_and_resolve_by_horizon(self) -> None:
        self.store.record_event(
            build_event(),
            {
                "support_price": 98.0,
                "resistance_price": 106.0,
                "spread_bps": 1.2,
            },
        )
        initial = self.store.recent(1)[0]
        self.assertEqual([item["status"] for item in initial["followups"]], ["pending"] * 5)
        self.assertTrue(initial["decision"]["directional"])
        self.assertLess(initial["decision"]["invalidation_price"], 98.0)
        self.assertEqual(initial["decision"]["target_price"], 106.0)
        self.assertEqual(initial["decision"]["invalidation_basis"], "结构支撑")
        self.assertEqual(initial["decision"]["target_basis"], "结构压力")
        self.assertIn(initial["decision"]["boundary_quality"], {"medium", "high"})
        self.assertGreater(initial["decision"]["buffer_components"]["mark_premium_bps"], 0)
        self.assertIn("放量", initial["trigger_combo"]["label"])

        for updated_at, price in (
            (1060.0, 101.0),
            (1120.0, 99.0),
            (1180.0, 103.0),
            (1240.0, 104.0),
            (1300.0, 102.0),
        ):
            self.store.record_snapshot(build_snapshot(updated_at, price))

        after_5m = self.store.recent(1)[0]
        followup_5m = after_5m["followups"][0]
        self.assertEqual(followup_5m["label"], "5m")
        self.assertEqual(followup_5m["status"], "resolved")
        self.assertAlmostEqual(followup_5m["close_bps"], 200.0, places=3)
        self.assertAlmostEqual(followup_5m["max_up_bps"], 400.0, places=3)
        self.assertAlmostEqual(followup_5m["max_down_bps"], -100.0, places=3)
        self.assertAlmostEqual(followup_5m["directional_close_bps"], 200.0, places=3)
        self.assertAlmostEqual(followup_5m["max_favorable_bps"], 400.0, places=3)
        self.assertAlmostEqual(followup_5m["max_adverse_bps"], 100.0, places=3)
        self.assertEqual(followup_5m["verdict"], "validated")
        self.assertEqual(followup_5m["sample_count"], 5)
        self.assertEqual(after_5m["followups"][1]["status"], "pending")

        self.store.record_snapshot(build_snapshot(1900.0, 110.0))

        after_15m = self.store.recent(1)[0]
        followup_15m = after_15m["followups"][1]
        self.assertEqual(followup_15m["label"], "15m")
        self.assertEqual(followup_15m["status"], "resolved")
        self.assertAlmostEqual(followup_15m["close_bps"], 1000.0, places=3)
        self.assertAlmostEqual(followup_15m["max_up_bps"], 1000.0, places=3)
        self.assertAlmostEqual(followup_15m["max_down_bps"], -100.0, places=3)
        self.assertEqual(followup_15m["sample_count"], 6)
        self.assertEqual(followup_15m["verdict"], "validated")
        self.assertEqual(after_15m["signal_stats"]["sample_count"], 1)
        self.assertAlmostEqual(after_15m["signal_stats"]["win_rate"], 100.0)
        self.assertAlmostEqual(after_15m["signal_stats"]["avg_close_bps"], 1000.0)
        self.assertEqual(after_15m["combo_stats"]["sample_count"], 1)
        self.assertIn("放量", after_15m["combo_stats"]["combo_label"])

        context = self.store.signal_context(after_15m)
        self.assertEqual(context["combo_stats"]["sample_count"], 1)
        self.assertIn("trigger_combo", context)

    def test_resolve_pending_followups_resolves_due_rows_without_snapshot_path(self) -> None:
        self.store.record_event(
            build_event(),
            {"support_price": 98.0, "resistance_price": 106.0},
        )
        for updated_at, price in [
            (1060.0, 101.0),
            (1120.0, 102.0),
            (1180.0, 104.0),
            (1240.0, 99.0),
            (1300.0, 102.0),
        ]:
            self.store.record_snapshot(
                build_snapshot(updated_at, price),
                resolve_followups=False,
            )

        pending = self.store.recent(1)[0]["followups"]
        self.assertTrue(all(item["status"] == "pending" for item in pending))

        self.assertTrue(self.store.resolve_pending_followups(now_ts=1301.0))

        followup_5m = self.store.recent(1)[0]["followups"][0]
        self.assertEqual(followup_5m["label"], "5m")
        self.assertEqual(followup_5m["status"], "resolved")
        self.assertEqual(followup_5m["source"], "signal_snapshots")
        self.assertAlmostEqual(followup_5m["close_bps"], 200.0, places=3)
        self.assertAlmostEqual(followup_5m["max_up_bps"], 400.0, places=3)
        self.assertAlmostEqual(followup_5m["max_down_bps"], -100.0, places=3)
        self.assertEqual(followup_5m["sample_count"], 5)

    def test_followup_resolver_takes_precedence_over_snapshot_sampling(self) -> None:
        calls = []

        def resolver(request: dict) -> dict | None:
            calls.append(request)
            if int(request["horizon_minutes"]) != 5:
                return None
            return {
                "source": "exchange_klines",
                "target_time": request["target_time"],
                "close_time": 1300.0,
                "anchor_price": request["anchor_price"],
                "close_price": 106.0,
                "high_price": 109.0,
                "low_price": 98.0,
                "close_bps": 600.0,
                "max_up_bps": 900.0,
                "max_down_bps": -200.0,
                "sample_count": 6,
                "mark_close_bps": 550.0,
            }

        store = AlertStore(
            str(self.db_path),
            snapshot_interval_seconds=60,
            followup_resolver=resolver,
        )
        store.record_event(build_event())
        store.record_snapshot(build_snapshot(1300.0, 102.0))

        followup_5m = store.recent(1)[0]["followups"][0]
        self.assertEqual(followup_5m["status"], "resolved")
        self.assertEqual(followup_5m["source"], "exchange_klines")
        self.assertAlmostEqual(followup_5m["close_bps"], 600.0)
        self.assertAlmostEqual(followup_5m["mark_close_bps"], 550.0)
        self.assertEqual(calls[0]["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
