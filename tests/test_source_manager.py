import unittest

from monitor.source_manager import build_source_specs, normalized_data_source


class SourceManagerTests(unittest.TestCase):
    def test_build_source_specs_adds_default_failover_candidate(self) -> None:
        specs = build_source_specs(
            {"failover": {"enabled": True}},
            exchange="binance_usdm",
            data_source="websocket",
        )

        self.assertEqual(
            [(spec.exchange, spec.data_source) for spec in specs],
            [("binance_usdm", "websocket"), ("okx_swap", "rest")],
        )

    def test_build_source_specs_deduplicates_candidates_and_keeps_configured_order(self) -> None:
        specs = build_source_specs(
            {
                "failover": {
                    "enabled": True,
                    "candidates": [
                        {"exchange": "okx_swap", "data_source": "websocket"},
                        {"exchange": "binance_usdm", "data_source": "websocket"},
                        {"exchange": "okx_swap", "data_source": "rest"},
                    ],
                }
            },
            exchange="binance_usdm",
            data_source="websocket",
        )

        self.assertEqual(
            [(spec.exchange, spec.data_source) for spec in specs],
            [("okx_swap", "rest"), ("binance_usdm", "websocket")],
        )

    def test_okx_forces_rest_data_source(self) -> None:
        self.assertEqual(normalized_data_source("okx_swap", "websocket"), "rest")


if __name__ == "__main__":
    unittest.main()
