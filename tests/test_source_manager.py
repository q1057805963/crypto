import asyncio
import time
import unittest
from unittest.mock import patch

from monitor.source_manager import (
    SourceContext,
    SourceFailoverManager,
    SourceSpec,
    build_source_specs,
    normalized_data_source,
)


class FakeStream:
    async def listen(self):
        while True:
            await asyncio.sleep(3600)


class FakeMicrostructureState:
    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)

    def liquidation_summary(self, symbol: str, seconds: int, now=None) -> dict:
        return {}


class SourceManagerTests(unittest.TestCase):
    def test_build_source_specs_adds_default_failover_candidate(self) -> None:
        specs = build_source_specs(
            {"failover": {"enabled": True}},
            exchange="okx_swap",
            data_source="websocket",
        )

        self.assertEqual(
            [(spec.exchange, spec.data_source) for spec in specs],
            [("okx_swap", "websocket"), ("okx_swap", "rest")],
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
            [("okx_swap", "websocket"), ("binance_usdm", "websocket"), ("okx_swap", "rest")],
        )

    def test_okx_supports_websocket_data_source(self) -> None:
        self.assertEqual(normalized_data_source("okx_swap", "auto"), "websocket")
        self.assertEqual(normalized_data_source("okx_swap", "websocket"), "websocket")
        self.assertEqual(normalized_data_source("okx_swap", "rest"), "rest")

    def test_source_health_falls_back_to_last_trade_age(self) -> None:
        manager = SourceFailoverManager(
            config={},
            specs=[SourceSpec("okx_swap", "rest")],
            symbols=["BTCUSDT"],
            stale_after_seconds=20,
            switch_cooldown_seconds=45,
        )
        manager._active_context = SourceContext(
            exchange="okx_swap",
            data_source="rest",
            stream=object(),
            microstructure_state=None,
            microstructure_stream=None,
        )
        manager._last_trade_at = time.monotonic()

        health = manager.source_health()

        self.assertEqual(health["label"], "OKX REST")
        self.assertEqual(health["status"], "active")
        self.assertEqual(health["active_count"], 1)
        self.assertEqual(health["total_count"], 1)
        self.assertEqual(health["channels"][0]["label"], "行情")

    def test_set_symbols_reconnects_active_source_with_new_symbols(self) -> None:
        activations = []

        def fake_build_source_context(config, symbols, spec):
            activations.append(list(symbols))
            return SourceContext(
                exchange=spec.exchange,
                data_source=spec.data_source,
                stream=FakeStream(),
                microstructure_state=FakeMicrostructureState(),
                microstructure_stream=None,
            )

        async def run_test() -> None:
            manager = SourceFailoverManager(
                config={},
                specs=[SourceSpec("okx_swap", "websocket")],
                symbols=["BTCUSDT"],
                stale_after_seconds=20,
                switch_cooldown_seconds=45,
            )
            with patch("monitor.source_manager.build_source_context", fake_build_source_context):
                await manager._activate(0)
                self.assertEqual(activations, [["BTCUSDT"]])
                self.assertEqual(manager._nonce, 1)

                manager.set_symbols(["BTCUSDT", "ETHUSDT"])
                await manager._reload_if_requested()

                self.assertEqual(activations, [["BTCUSDT"], ["BTCUSDT", "ETHUSDT"]])
                self.assertEqual(manager._nonce, 2)
                await manager.close()

        asyncio.run(run_test())

    def test_set_symbols_same_list_does_not_reconnect_active_source(self) -> None:
        activations = []

        def fake_build_source_context(config, symbols, spec):
            activations.append(list(symbols))
            return SourceContext(
                exchange=spec.exchange,
                data_source=spec.data_source,
                stream=FakeStream(),
                microstructure_state=FakeMicrostructureState(),
                microstructure_stream=None,
            )

        async def run_test() -> None:
            manager = SourceFailoverManager(
                config={},
                specs=[SourceSpec("okx_swap", "websocket")],
                symbols=["BTCUSDT"],
                stale_after_seconds=20,
                switch_cooldown_seconds=45,
            )
            with patch("monitor.source_manager.build_source_context", fake_build_source_context):
                await manager._activate(0)
                manager.set_symbols(["BTCUSDT"])
                await manager._reload_if_requested()

                self.assertEqual(activations, [["BTCUSDT"]])
                self.assertEqual(manager._nonce, 1)
                await manager.close()

        asyncio.run(run_test())


    def test_retries_primary_source_after_interval(self) -> None:
        def fake_build_source_context(config, symbols, spec):
            return SourceContext(
                exchange=spec.exchange,
                data_source=spec.data_source,
                stream=FakeStream(),
                microstructure_state=FakeMicrostructureState(),
                microstructure_stream=None,
            )

        async def run_test() -> None:
            switches = []
            manager = SourceFailoverManager(
                config={},
                specs=[SourceSpec("okx_swap", "websocket"), SourceSpec("okx_swap", "rest")],
                symbols=["BTCUSDT"],
                stale_after_seconds=20,
                switch_cooldown_seconds=45,
                primary_retry_seconds=300,
                on_switch=lambda exchange, source, note: switches.append((exchange, source, note)),
            )
            with patch("monitor.source_manager.build_source_context", fake_build_source_context):
                await manager._activate(1)
                manager._last_switch_at = time.monotonic() - 301

                await manager._maybe_retry_primary()

                self.assertEqual(manager._active_index, 0)
                self.assertEqual(manager.active_data_source, "websocket")
                self.assertIn("尝试切回主数据源", switches[-1][2])
                await manager.close()

        asyncio.run(run_test())

    def test_primary_retry_waits_for_interval_and_can_be_disabled(self) -> None:
        def fake_build_source_context(config, symbols, spec):
            return SourceContext(
                exchange=spec.exchange,
                data_source=spec.data_source,
                stream=FakeStream(),
                microstructure_state=FakeMicrostructureState(),
                microstructure_stream=None,
            )

        async def run_test() -> None:
            manager = SourceFailoverManager(
                config={},
                specs=[SourceSpec("okx_swap", "websocket"), SourceSpec("okx_swap", "rest")],
                symbols=["BTCUSDT"],
                stale_after_seconds=20,
                switch_cooldown_seconds=45,
                primary_retry_seconds=300,
            )
            with patch("monitor.source_manager.build_source_context", fake_build_source_context):
                await manager._activate(1)

                manager._last_switch_at = time.monotonic() - 10
                await manager._maybe_retry_primary()
                self.assertEqual(manager._active_index, 1)

                manager.primary_retry_seconds = 0.0
                manager._last_switch_at = time.monotonic() - 10_000
                await manager._maybe_retry_primary()
                self.assertEqual(manager._active_index, 1)
                await manager.close()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
