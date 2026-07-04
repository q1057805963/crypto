import unittest

from monitor.dashboard_state import build_review_stats


class DashboardReviewStatsTests(unittest.TestCase):
    def test_build_review_stats_groups_resolved_followups_by_direction(self) -> None:
        stats = build_review_stats(
            [
                {
                    "symbol": "BTCUSDT",
                    "direction": "up",
                    "followups": [
                        {
                            "horizon_minutes": 5,
                            "status": "resolved",
                            "close_bps": 100.0,
                            "max_up_bps": 150.0,
                            "max_down_bps": -40.0,
                        },
                        {"horizon_minutes": 15, "status": "pending"},
                    ],
                },
                {
                    "symbol": "ETHUSDT",
                    "direction": "down",
                    "followups": [
                        {
                            "horizon_minutes": 5,
                            "status": "resolved",
                            "close_bps": -50.0,
                            "max_up_bps": 30.0,
                            "max_down_bps": -120.0,
                        }
                    ],
                },
            ]
        )

        self.assertEqual(stats["event_count"], 2)
        self.assertEqual(stats["resolved_count"], 2)
        self.assertEqual([group["label"] for group in stats["groups"]], ["全部报警", "上涨报警", "下跌报警"])
        overall_5m = stats["groups"][0]["periods"][0]
        self.assertEqual(overall_5m["count"], 2)
        self.assertAlmostEqual(overall_5m["avg_close_bps"], 25.0)
        self.assertAlmostEqual(overall_5m["positive_rate_pct"], 50.0)

    def test_build_review_stats_omits_groups_without_resolved_followups(self) -> None:
        stats = build_review_stats(
            [
                {
                    "symbol": "BTCUSDT",
                    "direction": "up",
                    "followups": [{"horizon_minutes": 5, "status": "pending"}],
                }
            ]
        )

        self.assertEqual(stats["event_count"], 1)
        self.assertEqual(stats["resolved_count"], 0)
        self.assertEqual(stats["groups"], [])


if __name__ == "__main__":
    unittest.main()
