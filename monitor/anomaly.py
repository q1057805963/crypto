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
    spread_bps: float
    depth_imbalance: float
    bid_depth_notional: float
    ask_depth_notional: float
    depth_drop_pct_1m: float
    long_liquidation_quote_1m: float
    short_liquidation_quote_1m: float
    liquidation_total_quote_1m: float
    risk_level: str
    bias: str
    confidence: float
    reasons: tuple[str, ...]
    suggestions: tuple[str, ...]
    ai_analysis: str = ""
    ai_summary: tuple[str, ...] = ()


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
    spread_bps: float
    depth_imbalance: float
    bid_depth_notional: float
    ask_depth_notional: float
    depth_drop_pct_1m: float
    support_price: float
    resistance_price: float
    support_distance_pct: float
    resistance_distance_pct: float
    window_vwap: float
    vwap_deviation_pct: float
    range_position_pct: float
    bid_wall_price: float
    bid_wall_notional: float
    ask_wall_price: float
    ask_wall_notional: float
    long_liquidation_quote_1m: float
    short_liquidation_quote_1m: float
    liquidation_total_quote_1m: float
    liquidation_event_count_1m: int
    liquidation_data_status: str
    microstructure_status: str
    depth_data_age_seconds: float | None
    last_liquidation_age_seconds: float | None
    price_series_5m: tuple[float, ...]
    volume_series_5m: tuple[float, ...]
    oi_series_5m: tuple[float, ...]
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
        symbol_thresholds: dict | None = None,
    ) -> None:
        self.windows = {symbol.upper(): SymbolWindow(window_seconds) for symbol in symbols}
        self.started_at = time()
        self.window_seconds = window_seconds
        self.warmup_seconds = warmup_seconds
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.thresholds = thresholds
        self.symbol_thresholds: dict = symbol_thresholds or {}
        self.last_alert_at: dict[str, float] = {}

    def set_symbols(self, symbols: list[str]) -> None:
        wanted = {symbol.upper() for symbol in symbols}
        for symbol in wanted:
            self.windows.setdefault(symbol, SymbolWindow(self.window_seconds))
        for symbol in list(self.windows):
            if symbol not in wanted:
                del self.windows[symbol]
                self.last_alert_at.pop(symbol, None)

    def reset_windows(self, symbols: list[str] | None = None) -> None:
        target_symbols = (
            {symbol.upper() for symbol in symbols}
            if symbols is not None
            else set(self.windows)
        )
        for symbol in target_symbols:
            if symbol in self.windows:
                self.windows[symbol] = SymbolWindow(self.window_seconds)
            self.last_alert_at.pop(symbol, None)
        self.started_at = time()

    def set_symbol_threshold(self, symbol: str, score: float) -> None:
        self.symbol_thresholds[symbol.upper()] = {"anomaly_score": score}

    def remove_symbol_threshold(self, symbol: str) -> None:
        self.symbol_thresholds.pop(symbol.upper(), None)

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
            spread_bps=round(metrics["spread_bps"], 3),
            depth_imbalance=round(metrics["depth_imbalance"], 3),
            bid_depth_notional=round(metrics["bid_depth_notional"], 2),
            ask_depth_notional=round(metrics["ask_depth_notional"], 2),
            depth_drop_pct_1m=round(metrics["depth_drop_pct_1m"], 3),
            support_price=round(metrics["support_price"], 8),
            resistance_price=round(metrics["resistance_price"], 8),
            support_distance_pct=round(metrics["support_distance_pct"], 3),
            resistance_distance_pct=round(metrics["resistance_distance_pct"], 3),
            window_vwap=round(metrics["window_vwap"], 8),
            vwap_deviation_pct=round(metrics["vwap_deviation_pct"], 3),
            range_position_pct=round(metrics["range_position_pct"], 2),
            bid_wall_price=round(metrics["bid_wall_price"], 8),
            bid_wall_notional=round(metrics["bid_wall_notional"], 2),
            ask_wall_price=round(metrics["ask_wall_price"], 8),
            ask_wall_notional=round(metrics["ask_wall_notional"], 2),
            long_liquidation_quote_1m=round(metrics["long_liquidation_quote_1m"], 2),
            short_liquidation_quote_1m=round(metrics["short_liquidation_quote_1m"], 2),
            liquidation_total_quote_1m=round(metrics["liquidation_total_quote_1m"], 2),
            liquidation_event_count_1m=metrics["liquidation_event_count_1m"],
            liquidation_data_status=metrics["liquidation_data_status"],
            microstructure_status=metrics["microstructure_status"],
            depth_data_age_seconds=metrics["depth_data_age_seconds"],
            last_liquidation_age_seconds=metrics["last_liquidation_age_seconds"],
            price_series_5m=tuple(round(value, 8) for value in metrics["price_series_5m"]),
            volume_series_5m=tuple(round(value, 2) for value in metrics["volume_series_5m"]),
            oi_series_5m=tuple(round(value, 4) for value in metrics["oi_series_5m"]),
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

        threshold = float(
            self.symbol_thresholds.get(symbol, {}).get(
                "anomaly_score",
                self.thresholds.get("anomaly_score", 70),
            )
        )
        if metrics["score"] < threshold:
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
            spread_bps=round(metrics["spread_bps"], 3),
            depth_imbalance=round(metrics["depth_imbalance"], 3),
            bid_depth_notional=round(metrics["bid_depth_notional"], 2),
            ask_depth_notional=round(metrics["ask_depth_notional"], 2),
            depth_drop_pct_1m=round(metrics["depth_drop_pct_1m"], 3),
            long_liquidation_quote_1m=round(metrics["long_liquidation_quote_1m"], 2),
            short_liquidation_quote_1m=round(metrics["short_liquidation_quote_1m"], 2),
            liquidation_total_quote_1m=round(metrics["liquidation_total_quote_1m"], 2),
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
        spread_bps = float(trades_1m[-1].get("spread_bps") or 0)
        depth_imbalance = float(trades_1m[-1].get("depth_imbalance") or 0)
        bid_depth_notional = float(trades_1m[-1].get("bid_depth_notional") or 0)
        ask_depth_notional = float(trades_1m[-1].get("ask_depth_notional") or 0)
        depth_drop_pct_1m = float(trades_1m[-1].get("depth_drop_pct_1m") or 0)
        bid_wall_price = float(trades_1m[-1].get("bid_wall_price") or 0)
        bid_wall_notional = float(trades_1m[-1].get("bid_wall_notional") or 0)
        ask_wall_price = float(trades_1m[-1].get("ask_wall_price") or 0)
        ask_wall_notional = float(trades_1m[-1].get("ask_wall_notional") or 0)
        long_liquidation_quote_1m = float(trades_1m[-1].get("long_liquidation_quote_1m") or 0)
        short_liquidation_quote_1m = float(trades_1m[-1].get("short_liquidation_quote_1m") or 0)
        liquidation_total_quote_1m = float(trades_1m[-1].get("liquidation_total_quote_1m") or 0)
        liquidation_event_count_1m = int(trades_1m[-1].get("liquidation_event_count_1m") or 0)
        liquidation_data_status = str(trades_1m[-1].get("liquidation_data_status") or "unavailable")
        microstructure_status = str(trades_1m[-1].get("microstructure_status") or "unavailable")
        depth_data_age_seconds = trades_1m[-1].get("depth_data_age_seconds")
        last_liquidation_age_seconds = trades_1m[-1].get("last_liquidation_age_seconds")

        quote_volume_1m = sum(trade["quote_quantity"] for trade in trades_1m)
        quote_volume_window = sum(trade["quote_quantity"] for trade in window.trades)
        base_volume_window = sum(float(trade.get("quantity") or 0) for trade in window.trades)
        window_minutes = max(self.window_seconds / 60, 1)
        baseline_per_minute = max(quote_volume_window / window_minutes, 1)
        volume_multiplier = quote_volume_1m / baseline_per_minute
        window_vwap = (quote_volume_window / base_volume_window) if base_volume_window > 0 else latest_price
        vwap_deviation_pct = self._pct_change(window_vwap, latest_price)
        support_price = min(float(trade.get("price") or latest_price) for trade in window.trades)
        resistance_price = max(float(trade.get("price") or latest_price) for trade in window.trades)
        support_distance_pct = self._distance_below_pct(latest_price, support_price)
        resistance_distance_pct = self._distance_above_pct(latest_price, resistance_price)
        range_position_pct = self._range_position_pct(latest_price, support_price, resistance_price)

        buy_volume_1m = sum(
            trade["quote_quantity"] for trade in trades_1m if trade["side"] == "buy"
        )
        sell_volume_1m = max(quote_volume_1m - buy_volume_1m, 0)
        taker_buy_ratio = buy_volume_1m / quote_volume_1m if quote_volume_1m else 0.5
        price_series_5m, volume_series_5m, oi_series_5m = self._series(window, now)

        score, reasons = self._score(
            price_move_pct_1m=price_move_pct_1m,
            price_move_pct_5m=price_move_pct_5m,
            quote_volume_1m=quote_volume_1m,
            volume_multiplier=volume_multiplier,
            taker_buy_ratio=taker_buy_ratio,
            oi_change_pct_5m=oi_change_pct_5m,
            funding_rate=funding_rate,
            spread_bps=spread_bps,
            depth_imbalance=depth_imbalance,
            depth_drop_pct_1m=depth_drop_pct_1m,
            long_liquidation_quote_1m=long_liquidation_quote_1m,
            short_liquidation_quote_1m=short_liquidation_quote_1m,
        )
        direction = self._direction(price_move_pct_1m, buy_volume_1m, sell_volume_1m)
        bias = self._bias(
            direction,
            price_move_pct_5m,
            oi_change_pct_5m,
            funding_rate,
            long_liquidation_quote_1m,
            short_liquidation_quote_1m,
            spread_bps,
            depth_drop_pct_1m,
        )
        risk_level = self._risk_level(score)
        confidence = self._confidence(
            score,
            open_interest,
            funding_rate,
            reasons,
            liquidation_total_quote_1m,
            spread_bps,
        )
        suggestions = self._suggestions(
            bias=bias,
            direction=direction,
            price_move_pct_1m=price_move_pct_1m,
            price_move_pct_5m=price_move_pct_5m,
            volume_multiplier=volume_multiplier,
            oi_change_pct_5m=oi_change_pct_5m,
            funding_rate=funding_rate,
            depth_drop_pct_1m=depth_drop_pct_1m,
            long_liquidation_quote_1m=long_liquidation_quote_1m,
            short_liquidation_quote_1m=short_liquidation_quote_1m,
            spread_bps=spread_bps,
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
            "spread_bps": spread_bps,
            "depth_imbalance": depth_imbalance,
            "bid_depth_notional": bid_depth_notional,
            "ask_depth_notional": ask_depth_notional,
            "depth_drop_pct_1m": depth_drop_pct_1m,
            "support_price": support_price,
            "resistance_price": resistance_price,
            "support_distance_pct": support_distance_pct,
            "resistance_distance_pct": resistance_distance_pct,
            "window_vwap": window_vwap,
            "vwap_deviation_pct": vwap_deviation_pct,
            "range_position_pct": range_position_pct,
            "bid_wall_price": bid_wall_price,
            "bid_wall_notional": bid_wall_notional,
            "ask_wall_price": ask_wall_price,
            "ask_wall_notional": ask_wall_notional,
            "long_liquidation_quote_1m": long_liquidation_quote_1m,
            "short_liquidation_quote_1m": short_liquidation_quote_1m,
            "liquidation_total_quote_1m": liquidation_total_quote_1m,
            "liquidation_event_count_1m": liquidation_event_count_1m,
            "liquidation_data_status": liquidation_data_status,
            "microstructure_status": microstructure_status,
            "depth_data_age_seconds": depth_data_age_seconds,
            "last_liquidation_age_seconds": last_liquidation_age_seconds,
            "price_series_5m": price_series_5m,
            "volume_series_5m": volume_series_5m,
            "oi_series_5m": oi_series_5m,
            "risk_level": risk_level,
            "bias": bias,
            "confidence": confidence,
            "reasons": reasons,
            "suggestions": suggestions,
        }

    def _series(
        self,
        window: SymbolWindow,
        now: float,
        seconds: int = 300,
        buckets: int = 15,
    ) -> tuple[list[float], list[float], list[float]]:
        trades = window.since(now, seconds)
        if not trades:
            return [], [], []

        start = now - seconds
        bucket_width = max(seconds / buckets, 1)
        latest_price = float(trades[-1].get("price") or 0)
        latest_oi = float(trades[-1].get("open_interest") or 0)
        price_series: list[float | None] = [None] * buckets
        volume_series = [0.0] * buckets
        oi_series: list[float | None] = [None] * buckets

        for trade in trades:
            trade_time = float(trade.get("event_time") or now)
            raw_index = int((trade_time - start) / bucket_width)
            index = max(0, min(buckets - 1, raw_index))
            price_series[index] = float(trade.get("price") or latest_price)
            volume_series[index] += float(trade.get("quote_quantity") or 0)
            open_interest = trade.get("open_interest")
            if open_interest not in (None, ""):
                oi_series[index] = float(open_interest)

        price_seed = next((value for value in price_series if value is not None), latest_price)
        price_carry = price_seed
        for index, value in enumerate(price_series):
            if value is None:
                price_series[index] = price_carry
            else:
                price_carry = value

        oi_seed = next((value for value in oi_series if value is not None), latest_oi)
        oi_carry = oi_seed
        for index, value in enumerate(oi_series):
            if value is None:
                oi_series[index] = oi_carry
            else:
                oi_carry = value

        return (
            [float(value or 0) for value in price_series],
            volume_series,
            [float(value or 0) for value in oi_series],
        )

    def _score(
        self,
        price_move_pct_1m: float,
        price_move_pct_5m: float,
        quote_volume_1m: float,
        volume_multiplier: float,
        taker_buy_ratio: float,
        oi_change_pct_5m: float,
        funding_rate: float,
        spread_bps: float,
        depth_imbalance: float,
        depth_drop_pct_1m: float,
        long_liquidation_quote_1m: float,
        short_liquidation_quote_1m: float,
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
        liquidation_threshold = float(self.thresholds.get("liquidation_quote_1m", 250000))
        spread_threshold = float(self.thresholds.get("spread_bps", 4.0))
        depth_imbalance_threshold = float(self.thresholds.get("depth_imbalance_abs", 0.18))
        depth_drop_threshold = float(self.thresholds.get("depth_drop_pct_1m", 18.0))

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

        liquidation_total_quote_1m = long_liquidation_quote_1m + short_liquidation_quote_1m
        if liquidation_total_quote_1m >= liquidation_threshold:
            score += min(liquidation_total_quote_1m / liquidation_threshold * 16, 16)
            if long_liquidation_quote_1m > short_liquidation_quote_1m * 1.2:
                reasons.append(f"多头爆仓放大 {long_liquidation_quote_1m:,.0f} USDT")
            elif short_liquidation_quote_1m > long_liquidation_quote_1m * 1.2:
                reasons.append(f"空头爆仓放大 {short_liquidation_quote_1m:,.0f} USDT")
            else:
                reasons.append(f"双向爆仓放大 {liquidation_total_quote_1m:,.0f} USDT")

        if spread_bps >= spread_threshold:
            score += min(spread_bps / spread_threshold * 8, 8)
            reasons.append(f"盘口点差扩大 {spread_bps:.2f} bps")

        if abs(depth_imbalance) >= depth_imbalance_threshold:
            score += min(abs(depth_imbalance) / depth_imbalance_threshold * 6, 6)
            if depth_imbalance > 0:
                reasons.append(f"买盘深度占优 {depth_imbalance:+.2f}")
            else:
                reasons.append(f"卖盘深度占优 {depth_imbalance:+.2f}")

        if depth_drop_pct_1m >= depth_drop_threshold:
            score += min(depth_drop_pct_1m / depth_drop_threshold * 12, 12)
            reasons.append(f"盘口深度下降 {depth_drop_pct_1m:.1f}%")

        if quote_volume_1m < min_quote_volume_1m:
            reasons.append("1分钟成交额偏低，信号降权")

        return min(score, 100), reasons

    @staticmethod
    def _pct_change(old_price: float | None, new_price: float) -> float:
        if old_price is None or old_price <= 0:
            return 0.0
        return (new_price - old_price) / old_price * 100

    @staticmethod
    def _distance_below_pct(price: float, lower: float | None) -> float:
        if lower is None or lower <= 0 or price <= 0 or lower >= price:
            return 0.0
        return (price - lower) / price * 100

    @staticmethod
    def _distance_above_pct(price: float, upper: float | None) -> float:
        if upper is None or upper <= 0 or price <= 0 or upper <= price:
            return 0.0
        return (upper - price) / price * 100

    @staticmethod
    def _range_position_pct(price: float, low: float, high: float) -> float:
        if price <= 0 or high <= low:
            return 50.0
        return max(0.0, min((price - low) / (high - low) * 100, 100.0))

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
        long_liquidation_quote_1m: float,
        short_liquidation_quote_1m: float,
        spread_bps: float,
        depth_drop_pct_1m: float,
    ) -> str:
        if depth_drop_pct_1m >= 20 and spread_bps >= 4:
            return "插针风险：盘口明显变薄"
        if direction == "up" and short_liquidation_quote_1m > max(long_liquidation_quote_1m * 1.2, 0):
            return "偏多：疑似空头回补/逼空"
        if direction == "down" and long_liquidation_quote_1m > max(short_liquidation_quote_1m * 1.2, 0):
            return "偏空：疑似多头踩踏"
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
        liquidation_total_quote_1m: float,
        spread_bps: float,
    ) -> float:
        confidence = 25 + min(score * 0.55, 55) + min(len(reasons) * 4, 12)
        if open_interest:
            confidence += 5
        if funding_rate:
            confidence += 3
        if liquidation_total_quote_1m:
            confidence += 4
        if spread_bps:
            confidence += 2
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
        depth_drop_pct_1m: float,
        long_liquidation_quote_1m: float,
        short_liquidation_quote_1m: float,
        spread_bps: float,
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

        if long_liquidation_quote_1m > short_liquidation_quote_1m * 1.2 and long_liquidation_quote_1m > 0:
            suggestions.append("多头爆仓占优，留意是否出现被动砸盘后的超跌反弹")
        elif short_liquidation_quote_1m > long_liquidation_quote_1m * 1.2 and short_liquidation_quote_1m > 0:
            suggestions.append("空头爆仓占优，若价格仍能站稳，逼空延续概率会更高")

        if depth_drop_pct_1m >= 18 or spread_bps >= 4:
            suggestions.append("盘口正在变薄，追单前先确认点差和挂单深度是否恢复")

        if abs(price_move_pct_1m) >= 1 and abs(price_move_pct_5m) >= 2:
            suggestions.append("短周期波动已经较大，若参与需降低仓位并预设失效位置")

        return suggestions[:4]
