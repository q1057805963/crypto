import unittest

from monitor.telegram import TelegramAlert, normalize_telegram_users


class TelegramAlertFormatTests(unittest.TestCase):
    def snapshot(self) -> dict:
        return {
            "symbol": "BTCUSDT",
            "risk_level": "高风险",
            "bias": "偏多：疑似新增资金推动",
            "score": 62.7,
            "price": 100.0,
            "price_move_pct_1m": 1.2,
            "price_move_pct_5m": 2.4,
            "quote_volume_1m": 1200000,
            "volume_multiplier": 3.6,
            "oi_change_pct_5m": 2.1,
            "funding_rate": 0.0004,
            "spread_bps": 1.2,
            "depth_drop_pct_1m": 6.0,
            "short_liquidation_quote_1m": 50000,
            "long_liquidation_quote_1m": 0,
            "reasons": [
                "1分钟价格波动 +1.20%",
                "成交额放大 3.6x",
                "价格与放量共振",
            ],
            "suggestions": ["等待第二次放量确认"],
        }

    def test_default_push_message_explains_score_threshold_and_reasons(self) -> None:
        text = TelegramAlert._format_snapshot(
            self.snapshot(),
            {"default_score": 60, "symbol_thresholds": {}},
        )

        self.assertIn("推送条件:", text)
        self.assertIn("默认阈值: 异常分 62.7 >= 60.0", text)
        self.assertIn("触发原因:", text)
        self.assertIn("- 价格与放量共振", text)
        self.assertIn("观察建议:", text)

    def test_custom_push_rule_message_explains_matched_conditions(self) -> None:
        user = {
            "default_score": 60,
            "symbol_thresholds": {
                "BTCUSDT": {
                    "push_rules": {
                        "mode": "any",
                        "conditions": {
                            "volume_multiplier": {"enabled": True, "threshold": 2.2},
                            "price_move_pct_1m_abs": {"enabled": True, "threshold": 0.6},
                        },
                    }
                }
            },
        }

        text = TelegramAlert._format_snapshot(self.snapshot(), user)

        self.assertIn("推送规则: 任一附加条件满足", text)
        self.assertIn("量能倍数 3.60x >= 2.20x", text)
        self.assertIn("1分钟波动 1.20% >= 0.60%", text)

    def test_legacy_telegram_user_default_score_is_current_default(self) -> None:
        users = normalize_telegram_users(None, "token", ["123"])

        self.assertEqual(users[0]["default_score"], 60.0)


if __name__ == "__main__":
    unittest.main()
