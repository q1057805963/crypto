from datetime import datetime

from monitor.anomaly import AnomalyEvent


class ConsoleAlert:
    def send(self, event: AnomalyEvent) -> None:
        direction_text = {
            "up": "疑似向上异动",
            "down": "疑似向下异动",
            "mixed": "混合异常",
        }[event.direction]

        print("")
        print("=" * 72)
        print(
            f"[异常] {event.symbol} | 分数 {event.score}/100 | {event.risk_level} | {direction_text}"
        )
        print(f"倾向: {event.bias} | 置信度: {event.confidence:.1f}%")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"价格: {event.price}")
        print(f"1分钟波动: {event.price_move_pct_1m:+.3f}%")
        print(f"5分钟波动: {event.price_move_pct_5m:+.3f}%")
        print(f"1分钟成交额: {event.quote_volume_1m:,.0f} USDT")
        print(f"成交放大: {event.volume_multiplier:.2f}x")
        print(f"主动买入占比: {event.taker_buy_ratio_1m:.1%}")
        print(f"持仓量: {event.open_interest:,.4f}")
        print(f"5分钟OI变化: {event.oi_change_pct_5m:+.3f}%")
        print(f"资金费率: {event.funding_rate:.4%}")
        print(f"多头爆仓1m: {event.long_liquidation_quote_1m:,.0f} USDT")
        print(f"空头爆仓1m: {event.short_liquidation_quote_1m:,.0f} USDT")
        print(f"盘口点差: {event.spread_bps:.2f} bps")
        print(f"盘口深度下降: {event.depth_drop_pct_1m:.2f}%")
        print("原因: " + "; ".join(event.reasons))
        print("观察: " + "; ".join(event.suggestions))
        if event.ai_summary:
            print("AI: " + "; ".join(event.ai_summary))
        print("=" * 72)
        print("")
