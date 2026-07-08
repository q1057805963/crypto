import unittest
import unittest.mock

from monitor.instruments import (
    BinanceUsdtPerpDirectory,
    InstrumentDirectories,
    OkxUsdtSwapDirectory,
)


def make_directories(
    binance_symbols: set[str] | None,
    okx_symbols: set[str] | None,
) -> InstrumentDirectories:
    directories = InstrumentDirectories()
    directories.binance._symbols = binance_symbols
    directories.okx._symbols = okx_symbols
    return directories


class ValidateTests(unittest.TestCase):
    def test_missing_when_both_exchanges_confirm_absent(self) -> None:
        directories = make_directories({"BTCUSDT"}, {"BTCUSDT"})
        report = directories.validate(["BTCUSDT", "FAKEUSDT"], "binance")
        self.assertEqual(report["missing"], ["FAKEUSDT"])
        self.assertEqual(report["off_primary"], [])
        self.assertEqual(report["unchecked"], [])

    def test_chinese_binance_symbol_is_valid_when_listed(self) -> None:
        # 币安期货存在中文命名合约（如 币安人生USDT），不能按 ASCII 拒绝
        directories = make_directories({"BTCUSDT", "币安人生USDT"}, {"BTCUSDT"})
        report = directories.validate(["币安人生USDT"], "binance")
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["off_primary"], [])
        self.assertEqual(report["unchecked"], [])

    def test_chinese_symbol_missing_when_neither_exchange_lists_it(self) -> None:
        directories = make_directories({"BTCUSDT"}, {"BTCUSDT"})
        report = directories.validate(["币安人生USDT"], "binance")
        self.assertEqual(report["missing"], ["币安人生USDT"])

    def test_off_primary_when_only_secondary_exchange_has_it(self) -> None:
        directories = make_directories({"BTCUSDT"}, {"BTCUSDT", "RIVERUSDT"})
        report = directories.validate(["RIVERUSDT"], "binance")
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["off_primary"], ["RIVERUSDT"])
        self.assertEqual(report["primary_label"], "币安")
        self.assertEqual(report["secondary_label"], "OKX")

    def test_unchecked_when_directories_unavailable(self) -> None:
        directories = make_directories(None, None)
        report = directories.validate(["BTCUSDT", "币安人生USDT"], "binance")
        self.assertEqual(report["unchecked"], ["BTCUSDT", "币安人生USDT"])
        self.assertEqual(report["missing"], [])

    def test_single_absence_with_unknown_other_is_not_rejected(self) -> None:
        directories = make_directories({"BTCUSDT"}, None)
        report = directories.validate(["FAKEUSDT"], "binance")
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["unchecked"], ["FAKEUSDT"])

    def test_okx_primary_swaps_labels(self) -> None:
        directories = make_directories({"DOGEUSDT"}, {"BTCUSDT"})
        report = directories.validate(["DOGEUSDT"], "okx_swap")
        self.assertEqual(report["off_primary"], ["DOGEUSDT"])
        self.assertEqual(report["primary_label"], "OKX")
        self.assertEqual(report["secondary_label"], "币安")


class FilterTests(unittest.TestCase):
    def test_filters_symbols_missing_on_exchange(self) -> None:
        directories = make_directories(
            {"BTCUSDT", "ETHUSDT", "币安人生USDT"}, {"BTCUSDT", "RIVERUSDT"}
        )
        allowed, skipped = directories.filter_for_exchange(
            "binance", ["BTCUSDT", "RIVERUSDT", "币安人生USDT"]
        )
        self.assertEqual(allowed, ["BTCUSDT", "币安人生USDT"])
        self.assertEqual(skipped, ["RIVERUSDT"])

        allowed, skipped = directories.filter_for_exchange(
            "okx_swap", ["BTCUSDT", "RIVERUSDT", "币安人生USDT"]
        )
        self.assertEqual(allowed, ["BTCUSDT", "RIVERUSDT"])
        self.assertEqual(skipped, ["币安人生USDT"])

    def test_unavailable_directory_keeps_everything(self) -> None:
        directories = make_directories(None, None)
        allowed, skipped = directories.filter_for_exchange(
            "binance", ["BTCUSDT", "币安人生USDT"]
        )
        self.assertEqual(allowed, ["BTCUSDT", "币安人生USDT"])
        self.assertEqual(skipped, [])


class DirectoryParsingTests(unittest.TestCase):
    def test_binance_fetch_filters_non_perpetual_and_non_usdt(self) -> None:
        directory = BinanceUsdtPerpDirectory()
        payload = {
            "symbols": [
                {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "币安人生USDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCUSD_250926", "contractType": "CURRENT_QUARTER", "quoteAsset": "USD", "status": "TRADING"},
                {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING"},
                {"symbol": "ETHBTC", "contractType": "PERPETUAL", "quoteAsset": "BTC", "status": "TRADING"},
            ]
        }
        with unittest.mock.patch("monitor.instruments._get_json", return_value=payload):
            self.assertEqual(directory._fetch(), {"BTCUSDT", "币安人生USDT"})

    def test_okx_fetch_keeps_live_usdt_swaps(self) -> None:
        directory = OkxUsdtSwapDirectory()
        payload = {
            "code": "0",
            "data": [
                {"instId": "BTC-USDT-SWAP", "state": "live"},
                {"instId": "BTC-USD-SWAP", "state": "live"},
                {"instId": "OLD-USDT-SWAP", "state": "suspend"},
            ],
        }
        with unittest.mock.patch("monitor.instruments._get_json", return_value=payload):
            self.assertEqual(directory._fetch(), {"BTCUSDT"})


class BinanceStreamNamingTests(unittest.TestCase):
    def test_agg_trade_stream_names_support_chinese_symbols(self) -> None:
        from monitor.binance_ws import BinanceFuturesAggTradeStream

        stream = BinanceFuturesAggTradeStream(["BTCUSDT", "币安人生USDT"])
        self.assertEqual(stream.url, "wss://fstream.binance.com/stream")
        self.assertEqual(
            stream._streams,
            ["btcusdt@aggTrade", "币安人生usdt@aggTrade"],
        )

    def test_microstructure_stream_names_support_chinese_symbols(self) -> None:
        from monitor.microstructure import BinanceFuturesMicrostructureStream

        stream = BinanceFuturesMicrostructureStream(["币安人生USDT"], depth_levels=10, depth_interval="500ms")
        self.assertEqual(stream.url, "wss://fstream.binance.com/stream")
        self.assertEqual(
            stream._streams,
            ["币安人生usdt@depth10@500ms", "币安人生usdt@forceOrder"],
        )


if __name__ == "__main__":
    unittest.main()
