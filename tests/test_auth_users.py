import tempfile
import unittest
from pathlib import Path

from monitor.auth import AuthManager
from monitor.dashboard import user_admin_summary


class AuthUsersTests(unittest.TestCase):
    def test_public_users_do_not_expose_password_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            auth = AuthManager(
                {
                    "users_path": str(Path(temp_dir) / "auth_users.json"),
                    "secret_path": str(Path(temp_dir) / "auth_secret.key"),
                    "allow_registration": True,
                }
            )
            auth.register("admin", "password123")
            auth.register("trader", "password456")

            users = auth.public_users()

        self.assertEqual([user["username"] for user in users], ["admin", "trader"])
        self.assertTrue(all("password" not in user for user in users))
        self.assertTrue(all(user["user_id"].startswith("u_") for user in users))
        self.assertTrue(all("created_at" in user for user in users))

    def test_user_admin_summary_reports_runtime_configuration(self) -> None:
        summary = user_admin_summary(
            {
                "user_id": "u_123",
                "username": "admin",
                "role": "admin",
                "created_at": 1000,
            },
            {
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "telegram": {
                    "enabled": True,
                    "users": [
                        {
                            "enabled": True,
                            "bot_token": "token",
                            "chat_ids": ["1", "2"],
                        }
                    ],
                },
                "ai": {"enabled": True, "api_key": "secret"},
                "symbol_thresholds": {"BTCUSDT": {"anomaly_score": 65}},
                "updated_at": 1200.0,
            },
        )

        self.assertEqual(summary["username"], "admin")
        self.assertEqual(summary["symbol_count"], 2)
        self.assertTrue(summary["telegram_enabled"])
        self.assertEqual(summary["telegram_active_chat_count"], 2)
        self.assertTrue(summary["ai_enabled"])
        self.assertTrue(summary["ai_key_set"])
        self.assertEqual(summary["symbol_threshold_count"], 1)

    def test_user_admin_summary_supports_legacy_telegram_config(self) -> None:
        summary = user_admin_summary(
            {"user_id": "u_123", "username": "admin", "role": "admin"},
            {
                "symbols": ["BTCUSDT"],
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_ids": ["1", "2"],
                },
            },
        )

        self.assertEqual(summary["telegram_channel_count"], 1)
        self.assertEqual(summary["telegram_active_chat_count"], 2)


if __name__ == "__main__":
    unittest.main()
