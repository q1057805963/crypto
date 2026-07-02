from collections import deque
from dataclasses import dataclass
from time import time


@dataclass(frozen=True)
class AnomalyEvent:
    symbol: str
    score: float
    direction: str
    price: float
    price_move_pct_1m: float
    price_move_pct_5m: float
    quote_volume_1m: float
    volume_multiplier: float
    taker_buy_ratio_1m: float
    open_interest: float
    oi_change_pct_5m: float
    funding_rate: float
    risk_level: str
    bias: str
    confidence: float
    reasons: tuple[str, ...]
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    score: float
    direction: str
    price: float
    updated_at: float
    price_move_pct_1m: float
    price_move_pct_5m: float
    quote_volume_1m: float
    volume_multiplier: float
    taker_buy_ratio_1m: float
    trade_count_1m: int
    open_interest: float
    oi_change_pct_5m: float
    funding_rate: float
    risk_level: str
    bias: str
    confidence: float
    reasons: tuple[str, ...]
    suggestions: tuple[str, ...]


class SymbolWindow:
    def __init__(self, max_age_seconds: int) -> None:
        self.max_age_seconds = max_age_seconds
        self.trades = deque()

    def add(self, trade: dict) -> None:
        self.trades.append(trade)
        self.prune(trade["event_time"])

    def prune(self, now: float) -> None:
        cutoff = now - self.max_age_seconds
        while self.trades and self.trades[0]["event_time"] < cutoff:
            self.trades.popleft()

    def since(self, now: float, seconds: int) -> list[dict]:
        cutoff = now - seconds
        return [trade for trade in self.trades if trade["event_time"] >= cutoff]

    def first_price_since(self, now: float, seconds: int) -> float | None:
        trades = self.since(now, seconds)
        if not trades:
            return None
        return trades[0]["price"]

    def first_value_since(self, now: float, seconds: int, key: str) -> float | None:
        trades = self.since(now, seconds)
        for trade in trades:
            value = trade.get(key)
            if value:
                return float(value)
        return None


class AnomalyDetector:
    def __init__(
        self,
        symbols: list[str],
        window_seconds: int,
        warmup_seconds: int,
        alert_cooldown_seconds: int,
        thresholds: dict,
    ) -> None:
        self.windows = {symbol.upper(): SymbolWindow(window_seconds) for symbol in symbols}
        self.started_at = time()
        self.window_seconds = window_seconds
        self.warmup_seconds = warmup_seconds
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.thresholds = thresholds
        self.last_alert_at: dict[str, float] = {}

    def set_symbols(self, symbols: list[str]) -> None:
        wanted = {symbol.upper() for symbol in symbols}
        for symbol in wanted:
            self.windows.setdefault(symbol, SymbolWindow(self.window_seconds))
        for symbol in list(self.windows):
            if symbol not in wanted:
                del self.windows[symbol]
                self.last_alert_at.pop(symbol, None)

    def update(self, trade: dict) -> AnomalyEvent | None:
        symbol = trade["symbol"].upper()
        if symbol not in self.windows:
            return None

        window = self.windows[symbol]
        window.add(trade)
        now = trade["event_time"]

        if time() - self.started_at < self.warmup_seconds:
            return None

        event = self._evaluate(symbol, window, now)
        if not event:
            return None

        last_alert_at = self.last_alert_at.get(symbol, 0)
        if now - last_alert_at < self.alert_cooldown_seconds:
            return None

        self.last_alert_at[symbol] = now
        return event

    def snapshot(self, symbol: str) -> SymbolSnapshot | None:
        window = self.windows.get(symbol.upper())
        if not window or not window.trades:
            return None

        now = window.trades[-1]["event_time"]
        metrics = self._metrics(window, now)
        if not metrics:
            return None

        return SymbolSnapshot(
            symbol=symbol.upper(),
            score=round(metrics["score"], 1),
            direction=metrics["direction"],
            price=metrics["latest_price"],
            updated_at=now,
            price_move_pct_1m=round(metrics["price_move_pct_1m"], 3),
            price_move_pct_5m=round(metrics["price_move_pct_5m"], 3),
            quote_volume_1m=round(metrics["quote_volume_1m"], 2),
            volume_multiplier=round(metrics["volume_multiplier"], 2),
            taker_buy_ratio_1m=round(metrics["taker_buy_ratio"], 3),
            trade_count_1m=metrics["trade_count_1m"],
            open_interest=round(metrics["open_interest"], 4),
            oi_change_pct_5m=round(metrics["oi_change_pct_5m"], 3),
            funding_rate=round(metrics["funding_rate"], 8),
            risk_level=metrics["risk_level"],
            bias=metrics["bias"],
            confidence=round(metrics["confidence"], 1),
            reasons=tuple(metrics["reasons"]),
            suggestions=tuple(metrics["suggestions"]),
        )

    def _evaluate(self, symbol: str, window: SymbolWindow, now: float) -> AnomalyEvent | None:
        metrics = self._metrics(window, now)
        if not metrics or metrics["trade_count_1m"] < 3:
            return None

        if metrics["score"] < float(self.thresholds.get("anomaly_score", 70)):
            return None

        return AnomalyEvent(
            symbol=symbol,
            score=round(metrics["score"], 1),
            direction=metrics["direction"],
            price=metrics["latest_price"],
            price_move_pct_1m=round(metrics["price_move_pct_1m"], 3),
            price_move_pct_5m=round(metrics["price_move_pct_5m"], 3),
            quote_volume_1m=round(metrics["quote_volume_1m"], 2),
            volume_multiplier=round(metrics["volume_multiplier"], 2),
            taker_buy_ratio_1m=round(metrics["taker_buy_ratio"], 3),
            open_interest=round(metrics["open_interest"], 4),
            oi_change_pct_5m=round(metrics["oi_change_pct_5m"], 3),
            funding_rate=round(metrics["funding_rate"], 8),
            risk_level=metrics["risk_level"],
            bias=metrics["bias"],
            confidence=round(metrics["confidence"], 1),
            reasons=tuple(metrics["reasons"]),
            suggestions=tuple(metrics["suggestions"]),
        )

    def _metrics(self, window: SymbolWindow, now: float) -> dict | None:
        trades_1m = window.since(now, 60)
        if not trades_1m:
            return None

        latest_price = trades_1m[-1]["price"]
        price_1m_ago = window.first_price_since(now, 60)
        price_5m_ago = window.first_price_since(now, self.window_seconds)
        price_move_pct_1m = self._pct_change(price_1m_ago, latest_price)
        price_move_pct_5m = self._pct_change(price_5m_ago, latest_price)
        open_interest = float(trades_1m[-1].get("open_interest") or 0)
        open_interest_5m_ago = window.first_value_since(now, self.window_seconds, "open_interest")
        oi_change_pct_5m = self._pct_change(open_interest_5m_ago, open_interest)
        funding_rate = float(trades_1m[-1].get("funding_rate") or 0)

        quote_volume_1m = sum(trade["quote_quantity"] for trade in trades_1m)
        quote_volume_window = sum(trade["quote_quantity"] for trade in window.trades)
        window_minutes = max(self.window_seconds / 60, 1)
        baseline_per_minute = max(quote_volume_window / window_minutes, 1)
        volume_multiplier = quote_volume_1m / baseline_per_minute

        buy_volume_1m = sum(
            trade["quote_quantity"] for trade in trades_1m if trade["side"] == "buy"
        )
        sell_volume_1m = max(quote_volume_1m - buy_volume_1m, 0)
        taker_buy_ratio = buy_volume_1m / quote_volume_1m if quote_volume_1m else 0.5

        score, reasons = self._score(
            price_move_pct_1m=price_move_pct_1m,
            price_move_pct_5m=price_move_pct_5m,
            quote_volume_1m=quote_volume_1m,
            volume_multiplier=volume_multiplier,
            taker_buy_ratio=taker_buy_ratio,
            oi_change_pct_5m=oi_change_pct_5m,
            funding_rate=funding_rate,
        )
        direction = self._direction(price_move_pct_1m, buy_volume_1m, sell_volume_1m)
        bias = self._bias(direction, price_move_pct_5m, oi_change_pct_5m, funding_rate)
        risk_level = self._risk_level(score)
        confidence = self._confidence(score, open_interest, funding_rate, reasons)
        suggestions = self._suggestions(
            bias=bias,
            direction=direction,
            price_move_pct_1m=price_move_pct_1m,
            price_move_pct_5m=price_move_pct_5m,
            volume_multiplier=volume_multiplier,
            oi_change_pct_5m=oi_change_pct_5m,
            funding_rate=funding_rate,
        )

        return {
            "latest_price": latest_price,
            "price_move_pct_1m": price_move_pct_1m,
            "price_move_pct_5m": price_move_pct_5m,
            "quote_volume_1m": quote_volume_1m,
            "volume_multiplier": volume_multiplier,
            "buy_volume_1m": buy_volume_1m,
            "sell_volume_1m": sell_volume_1m,
            "taker_buy_ratio": taker_buy_ratio,
            "score": min(score, 100),
            "direction": direction,
            "trade_count_1m": len(trades_1m),
            "open_interest": open_interest,
            "oi_change_pct_5m": oi_change_pct_5m,
            "funding_rate": funding_rate,
            "risk_level": risk_level,
            "bias": bias,
            "confidence": confidence,
            "reasons": reasons,
            "suggestions": suggestions,
        }

    def _score(
        self,
        price_move_pct_1m: float,
        price_move_pct_5m: float,
        quote_volume_1m: float,
        volume_multiplier: float,
        taker_buy_ratio: float,
        oi_change_pct_5m: float,
        funding_rate: float,
    ) -> tuple[float, list[str]]:
        reasons = []
        score = 0.0

        min_quote_volume_1m = float(self.thresholds.get("min_quote_volume_1m", 0))
        liquidity_factor = 1.0 if quote_volume_1m >= min_quote_volume_1m else 0.45

        price_1m_threshold = float(self.thresholds.get("price_move_pct_1m", 0.8))
        price_5m_threshold = float(self.thresholds.get("price_move_pct_5m", 1.8))
        volume_threshold = float(self.thresholds.get("volume_multiplier", 3.0))
        buy_ratio_high = float(self.thresholds.get("taker_buy_ratio_high", 0.7))
        buy_ratio_low = float(self.thresholds.get("taker_buy_ratio_low", 0.3))
        oi_threshold = float(self.thresholds.get("oi_change_pct_5m", 1.5))
        funding_threshold = float(self.thresholds.get("funding_rate_abs", 0.0005))

        if abs(price_move_pct_1m) >= price_1m_threshold:
            score += min(abs(price_move_pct_1m) / price_1m_threshold * 24, 24) * liquidity_factor
            reasons.append(f"1分钟价格波动 {price_move_pct_1m:+.2f}%")

        if abs(price_move_pct_5m) >= price_5m_threshold:
            score += min(abs(price_move_pct_5m) / price_5m_threshold * 18, 18) * liquidity_factor
            reasons.append(f"5分钟价格波动 {price_move_pct_5m:+.2f}%")

        if volume_multiplier >= volume_threshold:
            score += min(volume_multiplier / volume_threshold * 22, 22)
            reasons.append(f"成交额放大 {volume_multiplier:.1f}x")

        if taker_buy_ratio >= buy_ratio_high:
            score += min((taker_buy_ratio - buy_ratio_high) / (1 - buy_ratio_high) * 14, 14)
            reasons.append(f"主动买入占比 {taker_buy_ratio:.0%}")
        elif taker_buy_ratio <= buy_ratio_low:
            score += min((buy_ratio_low - taker_buy_ratio) / buy_ratio_low * 14, 14)
            reasons.append(f"主动卖出占比 {1 - taker_buy_ratio:.0%}")

        if abs(oi_change_pct_5m) >= oi_threshold:
            score += min(abs(oi_change_pct_5m) / oi_threshold * 16, 16)
            reasons.append(f"持仓量变化 {oi_change_pct_5m:+.2f}%")

        if abs(funding_rate) >= funding_threshold:
            score += min(abs(funding_rate) / funding_threshold * 6, 6)
            side = "偏多拥挤" if funding_rate > 0 else "偏空拥挤"
            reasons.append(f"资金费率{side} {funding_rate:.4%}")

        if quote_volume_1m < min_quote_volume_1m:
            reasons.append("1分钟成交额偏低，信号降权")

        return min(score, 100), reasons

    @staticmethod
    def _pct_change(old_price: float | None, new_price: float) -> float:
        if old_price is None or old_price <= 0:
            return 0.0
        return (new_price - old_price) / old_price * 100

    @staticmethod
    def _direction(price_move_pct_1m: float, buy_volume_1m: float, sell_volume_1m: float) -> str:
        if price_move_pct_1m > 0 and buy_volume_1m >= sell_volume_1m:
            return "up"
        if price_move_pct_1m < 0 and sell_volume_1m > buy_volume_1m:
            return "down"
        return "mixed"

    @staticmethod
    def _risk_level(score: float) -> str:
        if score >= 85:
            return "极高风险"
        if score >= 70:
            return "高风险"
        if score >= 45:
            return "中风险"
        return "低风险"

    @staticmethod
    def _bias(
        direction: str,
        price_move_pct_5m: float,
        oi_change_pct_5m: float,
        funding_rate: float,
    ) -> str:
        if direction == "up" and oi_change_pct_5m >= 0.3:
            return "偏多：疑似新增资金推动"
        if direction == "down" and oi_change_pct_5m >= 0.3:
            return "偏空：疑似新增空头或连锁止损"
        if direction == "up":
            return "偏多：价格主动上行"
        if direction == "down":
            return "偏空：价格主动下行"
        if abs(funding_rate) >= 0.0005:
            return "拥挤：注意反向波动"
        if abs(price_move_pct_5m) >= 1:
            return "波动：方向待确认"
        return "观察：暂无明确方向"

    @staticmethod
    def _confidence(
        score: float,
        open_interest: float,
        funding_rate: float,
        reasons: list[str],
    ) -> float:
        confidence = 25 + min(score * 0.55, 55) + min(len(reasons) * 4, 12)
        if open_interest:
            confidence += 5
        if funding_rate:
            confidence += 3
        return min(confidence, 95)

    @staticmethod
    def _suggestions(
        bias: str,
        direction: str,
        price_move_pct_1m: float,
        price_move_pct_5m: float,
        volume_multiplier: float,
        oi_change_pct_5m: float,
        funding_rate: float,
    ) -> list[str]:
        suggestions = []

        if direction == "up":
            suggestions.append("观察回踩是否缩量，若回踩不破短线均价，说明买盘承接较强")
        elif direction == "down":
            suggestions.append("观察反弹是否无量，若反弹弱且持仓增加，需警惕继续下探")
        else:
            suggestions.append("方向暂不清晰，先看下一轮价格是否脱离当前区间")

        if oi_change_pct_5m >= 0.3:
            suggestions.append("持仓增加代表有新仓进入，重点看价格是否跟随持仓同向延续")
        elif oi_change_pct_5m <= -0.3:
            suggestions.append("持仓下降更像平仓推动，持续性通常弱于新增持仓行情")

        if volume_multiplier >= 3:
            suggestions.append("成交额明显放大，等待第二次放量确认，避免追在第一根脉冲顶部")

        if abs(funding_rate) >= 0.0005:
            suggestions.append("资金费率偏离，说明多空拥挤，注意反向清算或插针")

        if abs(price_move_pct_1m) >= 1 and abs(price_move_pct_5m) >= 2:
            suggestions.append("短周期波动已经较大，若参与需降低仓位并预设失效位置")

        return suggestions[:4]
