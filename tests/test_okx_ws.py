import json
import unittest

from monitor.microstructure import MarketMicrostructureState
from monitor.okx_ws import OkxSwapWebSocketStream


class OkxSwapWebSocketStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_health_summary_marks_active_stale_and_unavailable_channels(self) -> None:
        stream = OkxSwapWebSocketStream(
            ["BTCUSDT"],
            liquidation_poll_interval_seconds=999999,
        )

        stream._record_channel("BTCUSDT", "trades", event_time=100)
        stream._record_channel("BTCUSDT", "books5", event_time=1)
        health = stream.health_summary(now=105)
        channels = {item["key"]: item for item in health["channels"]}

        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["active_count"], 1)
        self.assertEqual(health["total_count"], 7)
        self.assertEqual(channels["trades"]["status"], "active")
        self.assertEqual(channels["trades"]["age_seconds"], 5)
        self.assertEqual(channels["books5"]["status"], "stale")
        self.assertEqual(channels["liquidations"]["status"], "unavailable")

    def test_health_summary_is_active_with_core_channels(self) -> None:
        stream = OkxSwapWebSocketStream(
            ["BTCUSDT"],
            liquidation_poll_interval_seconds=999999,
        )

        for channel in ["trades", "tickers", "books5"]:
            stream._record_channel("BTCUSDT", channel, event_time=100)

        health = stream.health_summary(now=105)

        self.assertEqual(health["status"], "active")
        self.assertEqual(health["active_count"], 3)

    async def test_parses_trade_with_state_and_microstructure_snapshot(self) -> None:
        microstructure_state = MarketMicrostructureState(["BTCUSDT"])
        stream = OkxSwapWebSocketStream(
            ["BTCUSDT"],
            liquidation_poll_interval_seconds=999999,
            microstructure_state=microstructure_state,
        )
        stream._rest_helper._contracts_to_base_quantity = lambda symbol, price, contracts: contracts * 0.01

        await stream._handle_message(
            json.dumps(
                {
                    "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
                    "data": [{"fundingRate": "0.0001"}],
                }
            )
        )
        await stream._handle_message(
            json.dumps(
                {
                    "arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
                    "data": [{"oiUsd": "123456"}],
                }
            )
        )
        await stream._handle_message(
            json.dumps(
                {
                    "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                    "data": [
                        {
                            "ts": "1000000",
                            "bids": [["49990", "2"]],
                            "asks": [["50010", "3"]],
                        }
                    ],
                }
            )
        )
        trade = await stream._handle_message(
            json.dumps(
                {
                    "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                    "data": [
                        {
                            "ts": "1001000",
                            "px": "50000",
                            "sz": "4",
                            "side": "buy",
                            "tradeId": "42",
                        }
                    ],
                }
            )
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade["symbol"], "BTCUSDT")
        self.assertEqual(trade["trade_id"], 42)
        self.assertAlmostEqual(trade["quantity"], 0.04)
        self.assertAlmostEqual(trade["quote_quantity"], 2000.0)
        self.assertEqual(trade["side"], "buy")
        self.assertAlmostEqual(trade["open_interest"], 123456.0)
        self.assertAlmostEqual(trade["funding_rate"], 0.0001)
        self.assertEqual(trade["microstructure_status"], "active")
        self.assertGreater(trade["spread_bps"], 0)


if __name__ == "__main__":
    unittest.main()
