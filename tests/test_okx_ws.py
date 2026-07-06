import asyncio
import json
import threading
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


    async def test_liquidation_poll_failures_are_throttled(self) -> None:
        microstructure_state = MarketMicrostructureState(["BTCUSDT"])
        stream = OkxSwapWebSocketStream(
            ["BTCUSDT"],
            liquidation_poll_interval_seconds=15,
            microstructure_state=microstructure_state,
        )
        calls = []

        def failing_fetch(symbol, now):
            calls.append(now)
            raise RuntimeError("network down")

        stream._rest_helper._fetch_liquidations = failing_fetch

        stream._maybe_poll_liquidations("BTCUSDT", 1000.0)
        first_task = stream._liquidation_tasks["BTCUSDT"]
        await first_task
        self.assertEqual(len(calls), 1)

        stream._maybe_poll_liquidations("BTCUSDT", 1005.0)
        self.assertIs(stream._liquidation_tasks["BTCUSDT"], first_task)
        self.assertEqual(len(calls), 1)

        stream._maybe_poll_liquidations("BTCUSDT", 1016.0)
        self.assertIsNot(stream._liquidation_tasks["BTCUSDT"], first_task)
        await stream._liquidation_tasks["BTCUSDT"]
        self.assertEqual(len(calls), 2)

    async def test_trade_handling_does_not_block_on_liquidation_poll(self) -> None:
        microstructure_state = MarketMicrostructureState(["BTCUSDT"])
        stream = OkxSwapWebSocketStream(
            ["BTCUSDT"],
            liquidation_poll_interval_seconds=1,
            microstructure_state=microstructure_state,
        )
        stream._rest_helper._contracts_to_base_quantity = lambda symbol, price, contracts: contracts * 0.01
        release = threading.Event()

        def slow_fetch(symbol, now):
            release.wait(timeout=5)
            return []

        stream._rest_helper._fetch_liquidations = slow_fetch

        trade = await asyncio.wait_for(
            stream._handle_message(
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
            ),
            timeout=1.0,
        )

        self.assertIsNotNone(trade)
        release.set()
        await stream._liquidation_tasks["BTCUSDT"]


if __name__ == "__main__":
    unittest.main()
