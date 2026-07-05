import unittest

from monitor.dashboard_state import DashboardState


class DashboardStateTests(unittest.TestCase):
    def test_payload_honors_requested_symbol_order(self):
        state = DashboardState(
            ["BTCUSDT", "ETHUSDT", "DOGEUSDT"],
            data_source="websocket",
        )

        payload = state.as_payload(["DOGEUSDT", "BTCUSDT"])

        self.assertEqual(
            [item["symbol"] for item in payload["symbols"]],
            ["DOGEUSDT", "BTCUSDT"],
        )

    def test_payload_preserves_state_symbol_order_without_filter(self):
        state = DashboardState(
            ["SOL", "BTC", "ETH"],
            data_source="websocket",
        )

        payload = state.as_payload()

        self.assertEqual(
            [item["symbol"] for item in payload["symbols"]],
            ["SOLUSDT", "BTCUSDT", "ETHUSDT"],
        )


if __name__ == "__main__":
    unittest.main()
