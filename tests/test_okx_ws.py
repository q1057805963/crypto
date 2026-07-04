import json
import unittest

from monitor.microstructure import MarketMicrostructureState
from monitor.okx_ws import OkxSwapWebSocketStream


class OkxSwapWebSocketStreamTests(unittest.IsolatedAsyncioTestCase):
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
