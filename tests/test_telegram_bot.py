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


if __name__ == "__main__":
    unittest.main()
