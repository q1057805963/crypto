import unittest

from monitor.telegram_bot import TelegramBotResponder


class TelegramBotResponderTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_fresh_update_is_processed_after_startup(self) -> None:
        handled = []
        responder = TelegramBotResponder(
            enabled=True,
            get_users=lambda: [{"enabled": True, "bot_token": "token", "chat_ids": ["1"]}],
            get_snapshot=lambda symbol: None,
            get_ai_analyzer=lambda owner_id: None,
        )
        responder._started_at = 1000
        responder._get_updates = lambda bot_token, offset: [
            {
                "update_id": 1,
                "message": {
                    "date": 1001,
                    "text": "BTC 怎么样",
                    "chat": {"id": "1"},
                },
            }
        ]

        async def fake_handle(bot_token, users, update):
            handled.append(update["update_id"])

        responder._handle_update = fake_handle

        await responder._poll_once()

        self.assertEqual(handled, [1])
        self.assertEqual(responder._offsets["token"], 2)

    async def test_old_backlog_is_skipped_on_first_poll(self) -> None:
        handled = []
        responder = TelegramBotResponder(
            enabled=True,
            get_users=lambda: [{"enabled": True, "bot_token": "token", "chat_ids": ["1"]}],
            get_snapshot=lambda symbol: None,
            get_ai_analyzer=lambda owner_id: None,
        )
        responder._started_at = 1000
        responder._get_updates = lambda bot_token, offset: [
            {
                "update_id": 1,
                "message": {
                    "date": 900,
                    "text": "旧消息",
                    "chat": {"id": "1"},
                },
            }
        ]

        async def fake_handle(bot_token, users, update):
            handled.append(update["update_id"])

        responder._handle_update = fake_handle

        await responder._poll_once()

        self.assertEqual(handled, [])
        self.assertEqual(responder._offsets["token"], 2)

    async def test_followup_reuses_recent_symbol_and_passes_history(self) -> None:
        sent = []
        context_calls = []

        class FakeAnalyzer:
            enabled = True
            api_key = "secret"

            def __init__(self) -> None:
                self.calls = []

            async def answer_question(
                self,
                question,
                snapshot,
                available_symbols,
                *,
                history=None,
                timeframe_data=None,
                confluence_data=None,
            ):
                self.calls.append(
                    {
                        "question": question,
                        "symbol": snapshot.get("symbol"),
                        "history": list(history or []),
                        "timeframe_data": timeframe_data,
                        "confluence_data": confluence_data,
                    }
                )
                return "**结论**：先别追，等回踩 61800 不破再看。"

            def get_last_error(self):
                return None

        analyzer = FakeAnalyzer()

        def fake_timeframe_context(symbol, period):
            context_calls.append((symbol, period))
            return {"period_label": period or "无"}, {"label": "共振"}

        users = [
            {
                "enabled": True,
                "bot_token": "token",
                "chat_ids": ["1"],
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "owner_id": "",
            }
        ]
        responder = TelegramBotResponder(
            enabled=True,
            get_users=lambda: users,
            get_snapshot=lambda symbol: {"symbol": symbol},
            get_ai_analyzer=lambda owner_id: analyzer,
            get_timeframe_context=fake_timeframe_context,
            ai_cooldown_seconds=0,
        )
        responder._send = lambda bot_token, chat_id, text: sent.append(text)
        responder._send_chat_action = lambda bot_token, chat_id: None

        await responder._handle_update(
            "token",
            users,
            {"message": {"text": "BTC 1小时现在能追吗", "chat": {"id": "1"}}},
        )
        await responder._handle_update(
            "token",
            users,
            {"message": {"text": "那止损放哪里？", "chat": {"id": "1"}}},
        )

        self.assertEqual(len(analyzer.calls), 2)
        self.assertEqual(analyzer.calls[1]["symbol"], "BTCUSDT")
        self.assertEqual(analyzer.calls[0]["history"], [])
        self.assertEqual(len(analyzer.calls[1]["history"]), 1)
        self.assertEqual(analyzer.calls[1]["history"][0]["question"], "BTC 1小时现在能追吗")
        self.assertNotIn("**", analyzer.calls[1]["history"][0]["answer"])
        self.assertEqual(context_calls[0], ("BTCUSDT", "1h"))
        self.assertEqual(context_calls[1], ("BTCUSDT", None))
        self.assertEqual(analyzer.calls[0]["timeframe_data"], {"period_label": "1h"})
        self.assertEqual(analyzer.calls[0]["confluence_data"], {"label": "共振"})
        self.assertEqual(len(sent), 2)
        for text in sent:
            self.assertNotIn("正在分析", text)
            self.assertNotIn("**", text)
            self.assertNotIn("可以直接问我合约问题", text)

    async def test_no_symbol_and_no_history_falls_back_to_help(self) -> None:
        sent = []
        users = [
            {
                "enabled": True,
                "bot_token": "token",
                "chat_ids": ["1"],
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "owner_id": "",
            }
        ]
        responder = TelegramBotResponder(
            enabled=True,
            get_users=lambda: users,
            get_snapshot=lambda symbol: {"symbol": symbol},
            get_ai_analyzer=lambda owner_id: None,
        )
        responder._send = lambda bot_token, chat_id, text: sent.append(text)

        await responder._handle_update(
            "token",
            users,
            {"message": {"text": "那现在怎么办？", "chat": {"id": "1"}}},
        )

        self.assertEqual(len(sent), 1)
        self.assertIn("可以直接问我合约问题", sent[0])

    def test_sanitize_reply_strips_markdown_markers(self) -> None:
        raw = "### 结论\n**先别追**，等回踩。\n* 观察 61800\n```python\nprint(1)\n```"
        cleaned = TelegramBotResponder._sanitize_reply(raw)

        self.assertNotIn("###", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("```", cleaned)
        self.assertIn("- 观察 61800", cleaned)
        self.assertIn("先别追", cleaned)

    def test_detect_period_matches_common_phrases(self) -> None:
        self.assertEqual(TelegramBotResponder._detect_period("BTC 15分钟怎么看"), "15m")
        self.assertEqual(TelegramBotResponder._detect_period("4小时结构如何"), "4h")
        self.assertEqual(TelegramBotResponder._detect_period("日线支撑在哪"), "1d")
        self.assertEqual(TelegramBotResponder._detect_period("ETH 5分图呢"), "5m")
        self.assertIsNone(TelegramBotResponder._detect_period("现在能追吗"))

    def test_extract_symbol_ignores_indicator_tokens_and_numbers(self) -> None:
        self.assertIsNone(TelegramBotResponder._extract_symbol("OI 涨这么多正常吗", []))
        self.assertIsNone(TelegramBotResponder._extract_symbol("15分钟怎么看", []))
        self.assertEqual(
            TelegramBotResponder._extract_symbol("15分钟BTC怎么看", []),
            "BTCUSDT",
        )


if __name__ == "__main__":
    unittest.main()
