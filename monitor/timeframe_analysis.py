import json
import threading
import time
from statistics import mean
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TIMEFRAME_CONFIG = {
    "5m": {"binance": "5m", "okx": "5m", "seconds": 300},
    "15m": {"binance": "15m", "okx": "15m", "seconds": 900},
    "1h": {"binance": "1h", "okx": "1H", "seconds": 3600},
    "4h": {"binance": "4h", "okx": "4H", "seconds": 14400},
    "1d": {"binance": "1d", "okx": "1Dutc", "seconds": 86400},
}

FOLLOWUP_INTERVALS = (
    (60, "1m", "1m", 60),
    (240, "5m", "5m", 300),
    (1440, "15m", "15m", 900),
)


def _pct_change(start: float, end: float) -> float:
    if start <= 0 or end <= 0:
        return 0.0
    return (end / start - 1) * 100


def _bps_change(start: float, end: float) -> float:
    if start <= 0 or end <= 0:
        return 0.0
    return (end / start - 1) * 10000


def _distance_below_pct(price: float, support: float) -> float:
    if price <= 0 or support <= 0:
        return 0.0
    return max((price / support - 1) * 100, 0.0)


def _distance_above_pct(price: float, resistance: float) -> float:
    if price <= 0 or resistance <= 0:
        return 0.0
    return max((resistance / price - 1) * 100, 0.0)


def _range_position_pct(price: float, support: float, resistance: float) -> float:
    if price <= 0 or support <= 0 or resistance <= support:
        return 50.0
    return min(100.0, max(0.0, ((price - support) / (resistance - support)) * 100))


def _normalized_exchange(exchange: str) -> str:
    value = str(exchange or "").strip().lower()
    return "okx" if value.startswith("okx") else "binance"


def _okx_inst_id(symbol: str) -> str:
    symbol = symbol.upper()
    if "-" in symbol:
        return symbol
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}-USDT-SWAP"


def _label_for_period(period: str) -> str:
    return period


def _followup_interval(horizon_minutes: int) -> tuple[str, str, int]:
    for max_minutes, binance_interval, okx_interval, seconds in FOLLOWUP_INTERVALS:
        if int(horizon_minutes) <= max_minutes:
            return binance_interval, okx_interval, seconds
    return "15m", "15m", 900


def build_followup_result(
    *,
    symbol: str,
    exchange: str,
    horizon_minutes: int,
    event_time: float,
    target_time: float,
    anchor_price: float,
    price_candles: list[dict],
    mark_candles: list[dict],
    interval_seconds: int,
) -> dict | None:
    candles = [
        item
        for item in price_candles
        if float(item.get("close_time") or item.get("open_time") or 0) >= event_time
        and float(item.get("open_time") or 0) <= target_time
    ]
    if not candles or anchor_price <= 0:
        return None

    close_candle = candles[-1]
    close_time = float(close_candle.get("close_time") or close_candle.get("open_time") or target_time)
    close_price = float(close_candle["close"])
    high_price = max([anchor_price] + [float(item["high"]) for item in candles])
    low_price = min([anchor_price] + [float(item["low"]) for item in candles])

    mark_window = [
        item
        for item in mark_candles
        if float(item.get("close_time") or item.get("open_time") or 0) >= event_time
        and float(item.get("open_time") or 0) <= target_time
    ]
    mark_payload = {}
    if mark_window:
        mark_anchor = float(mark_window[0]["open"])
        mark_close = float(mark_window[-1]["close"])
        mark_high = max(float(item["high"]) for item in mark_window)
        mark_low = min(float(item["low"]) for item in mark_window)
        mark_payload = {
            "mark_anchor_price": round(mark_anchor, 8),
            "mark_close_price": round(mark_close, 8),
            "mark_high_price": round(mark_high, 8),
            "mark_low_price": round(mark_low, 8),
            "mark_close_bps": round(_bps_change(mark_anchor, mark_close), 3),
            "mark_max_up_bps": round(_bps_change(mark_anchor, mark_high), 3),
            "mark_max_down_bps": round(_bps_change(mark_anchor, mark_low), 3),
            "mark_sample_count": len(mark_window),
        }

    close_bps = _bps_change(anchor_price, close_price)
    max_up_bps = _bps_change(anchor_price, high_price)
    max_down_bps = _bps_change(anchor_price, low_price)
    return {
        "symbol": symbol.upper(),
        "exchange": exchange,
        "source": "exchange_klines",
        "interval_seconds": int(interval_seconds),
        "label": _followup_label(horizon_minutes),
        "horizon_minutes": int(horizon_minutes),
        "event_time": float(event_time),
        "target_time": float(target_time),
        "close_time": close_time,
        "anchor_price": round(anchor_price, 8),
        "close_price": round(close_price, 8),
        "high_price": round(high_price, 8),
        "low_price": round(low_price, 8),
        "close_bps": round(close_bps, 3),
        "max_up_bps": round(max_up_bps, 3),
        "max_down_bps": round(max_down_bps, 3),
        "sample_count": len(candles),
        **mark_payload,
    }


def _followup_label(horizon_minutes: int) -> str:
    mapping = {
        5: "5m",
        15: "15m",
        60: "1h",
        240: "4h",
        1440: "1d",
    }
    return mapping.get(int(horizon_minutes), f"{int(horizon_minutes)}m")


def build_timeframe_analysis(
    *,
    symbol: str,
    period: str,
    exchange: str,
    price_candles: list[dict],
    mark_candles: list[dict],
) -> dict:
    if not price_candles:
        raise ValueError("no price candles")

    candles = price_candles[-24:]
    current = candles[-1]
    previous = next((item for item in reversed(candles[:-1]) if item.get("confirmed")), None)
    if previous is None and len(candles) >= 2:
        previous = candles[-2]
    previous = previous or current

    support_price = min(float(item["low"]) for item in candles)
    resistance_price = max(float(item["high"]) for item in candles)
    base_volume_window = sum(float(item.get("base_volume") or 0) for item in candles)
    quote_volume_window = sum(float(item.get("quote_volume") or 0) for item in candles)
    if base_volume_window > 0 and quote_volume_window > 0:
        window_vwap = quote_volume_window / base_volume_window
    else:
        window_vwap = mean(float(item["close"]) for item in candles)

    avg_volume_samples = [
        float(item.get("quote_volume") or 0)
        for item in candles[:-1]
        if float(item.get("quote_volume") or 0) > 0
    ]
    avg_quote_volume = mean(avg_volume_samples) if avg_volume_samples else float(current.get("quote_volume") or 0)
    quote_volume = float(current.get("quote_volume") or 0)
    volume_multiplier = (quote_volume / avg_quote_volume) if avg_quote_volume > 0 else 1.0

    close_price = float(current["close"])
    open_price = float(current["open"])
    high_price = float(current["high"])
    low_price = float(current["low"])
    prev_close = float(previous["close"])
    price_move_pct = _pct_change(open_price, close_price)
    prev_close_pct = _pct_change(prev_close, close_price)
    vwap_deviation_pct = _pct_change(window_vwap, close_price)
    support_distance_pct = _distance_below_pct(close_price, support_price)
    resistance_distance_pct = _distance_above_pct(close_price, resistance_price)
    range_position_pct = _range_position_pct(close_price, support_price, resistance_price)

    current_mark = mark_candles[-1] if mark_candles else None
    previous_mark = None
    if mark_candles:
        previous_mark = next((item for item in reversed(mark_candles[:-1]) if item.get("confirmed")), None)
        if previous_mark is None and len(mark_candles) >= 2:
            previous_mark = mark_candles[-2]
    previous_mark = previous_mark or current_mark
    mark_price = float(current_mark["close"]) if current_mark else None
    mark_move_pct = (
        _pct_change(float(current_mark["open"]), float(current_mark["close"]))
        if current_mark
        else None
    )
    mark_prev_close_pct = (
        _pct_change(float(previous_mark["close"]), float(current_mark["close"]))
        if current_mark and previous_mark
        else None
    )
    mark_premium_bps = (
        _bps_change(close_price, mark_price)
        if current_mark and mark_price and close_price > 0
        else None
    )

    return {
        "symbol": symbol.upper(),
        "period": period,
        "period_label": _label_for_period(period),
        "exchange": exchange,
        "generated_at": time.time(),
        "candle_confirmed": bool(current.get("confirmed", False)),
        "mark_confirmed": bool(current_mark.get("confirmed", False)) if current_mark else None,
        "open_time": float(current["open_time"]),
        "price": close_price,
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "prev_close_price": prev_close,
        "price_move_pct": round(price_move_pct, 3),
        "prev_close_pct": round(prev_close_pct, 3),
        "quote_volume": round(quote_volume, 2),
        "avg_quote_volume": round(avg_quote_volume, 2),
        "volume_multiplier": round(volume_multiplier, 2),
        "support_price": round(support_price, 8),
        "resistance_price": round(resistance_price, 8),
        "window_vwap": round(window_vwap, 8),
        "vwap_deviation_pct": round(vwap_deviation_pct, 3),
        "support_distance_pct": round(support_distance_pct, 3),
        "resistance_distance_pct": round(resistance_distance_pct, 3),
        "range_position_pct": round(range_position_pct, 2),
        "price_series": [round(float(item["close"]), 8) for item in candles],
        "volume_series": [round(float(item.get("quote_volume") or 0), 2) for item in candles],
        "mark_price": round(mark_price, 8) if mark_price is not None else None,
        "mark_move_pct": round(mark_move_pct, 3) if mark_move_pct is not None else None,
        "mark_prev_close_pct": round(mark_prev_close_pct, 3) if mark_prev_close_pct is not None else None,
        "mark_premium_bps": round(mark_premium_bps, 3) if mark_premium_bps is not None else None,
        "mark_price_series": (
            [round(float(item["close"]), 8) for item in mark_candles[-24:]]
            if mark_candles
            else []
        ),
    }


class TimeframeAnalysisService:
    def __init__(self, cache_ttl_seconds: int = 20, request_timeout_seconds: int = 10) -> None:
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.request_timeout_seconds = int(request_timeout_seconds)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, str], dict] = {}

    def analyze(self, symbol: str, period: str, exchange: str, force: bool = False) -> dict:
        symbol = str(symbol or "").upper()
        if not symbol:
            raise ValueError("symbol required")
        if period not in TIMEFRAME_CONFIG:
            raise ValueError("unsupported period")
        exchange_key = _normalized_exchange(exchange)
        cache_key = (exchange_key, symbol, period)
        if not force:
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached and time.time() - float(cached["cached_at"]) < self.cache_ttl_seconds:
                    return dict(cached["payload"])

        payload = self._fetch_and_build(symbol, period, exchange_key)
        with self._lock:
            self._cache[cache_key] = {"cached_at": time.time(), "payload": dict(payload)}
        return payload

    def resolve_followup(
        self,
        *,
        symbol: str,
        exchange: str,
        horizon_minutes: int,
        event_time: float,
        target_time: float,
        anchor_price: float,
    ) -> dict | None:
        symbol = str(symbol or "").upper()
        if not symbol:
            return None
        exchange_key = _normalized_exchange(exchange)
        binance_interval, okx_interval, interval_seconds = _followup_interval(horizon_minutes)
        if exchange_key == "okx":
            price_candles = self._fetch_okx_candles_range(
                symbol,
                okx_interval,
                event_time,
                target_time,
                interval_seconds,
                mark=False,
            )
            mark_candles = self._fetch_okx_candles_range(
                symbol,
                okx_interval,
                event_time,
                target_time,
                interval_seconds,
                mark=True,
            )
            exchange_name = "okx_swap"
        else:
            price_candles = self._fetch_binance_candles_range(
                symbol,
                binance_interval,
                event_time,
                target_time,
                mark=False,
            )
            mark_candles = self._fetch_binance_candles_range(
                symbol,
                binance_interval,
                event_time,
                target_time,
                mark=True,
            )
            exchange_name = "binance_usdm"
        return build_followup_result(
            symbol=symbol,
            exchange=exchange_name,
            horizon_minutes=horizon_minutes,
            event_time=event_time,
            target_time=target_time,
            anchor_price=anchor_price,
            price_candles=price_candles,
            mark_candles=mark_candles,
            interval_seconds=interval_seconds,
        )

    def _fetch_and_build(self, symbol: str, period: str, exchange_key: str) -> dict:
        interval = TIMEFRAME_CONFIG[period]
        if exchange_key == "okx":
            price_candles = self._fetch_okx_candles(symbol, interval["okx"], mark=False)
            mark_candles = self._fetch_okx_candles(symbol, interval["okx"], mark=True)
            exchange_name = "okx_swap"
        else:
            price_candles = self._fetch_binance_candles(symbol, interval["binance"], mark=False)
            mark_candles = self._fetch_binance_candles(symbol, interval["binance"], mark=True)
            exchange_name = "binance_usdm"

        return build_timeframe_analysis(
            symbol=symbol,
            period=period,
            exchange=exchange_name,
            price_candles=price_candles,
            mark_candles=mark_candles,
        )

    def _fetch_binance_candles(self, symbol: str, interval: str, mark: bool) -> list[dict]:
        endpoint = "markPriceKlines" if mark else "klines"
        query = urlencode({"symbol": symbol.upper(), "interval": interval, "limit": "24"})
        request = Request(
            f"https://fapi.binance.com/fapi/v1/{endpoint}?{query}",
            headers={"User-Agent": "crypto-futures-monitor/0.1"},
        )
        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        now_ms = int(time.time() * 1000)
        candles = []
        for item in payload or []:
            if len(item) < 5:
                continue
            open_time = float(item[0])
            close_time = float(item[6]) if len(item) > 6 else open_time
            candles.append(
                {
                    "open_time": open_time / 1000,
                    "close_time": close_time / 1000,
                    "close": float(item[4]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "base_volume": float(item[5] or 0) if len(item) > 5 else 0.0,
                    "quote_volume": float(item[7] or 0) if len(item) > 7 else 0.0,
                    "confirmed": close_time <= now_ms,
                }
            )
        return candles

    def _fetch_binance_candles_range(
        self,
        symbol: str,
        interval: str,
        event_time: float,
        target_time: float,
        mark: bool,
    ) -> list[dict]:
        endpoint = "markPriceKlines" if mark else "klines"
        start_ms = max(int((event_time - 60) * 1000), 0)
        end_ms = int((target_time + 60) * 1000)
        query = urlencode(
            {
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "1000",
            }
        )
        request = Request(
            f"https://fapi.binance.com/fapi/v1/{endpoint}?{query}",
            headers={"User-Agent": "crypto-futures-monitor/0.1"},
        )
        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        now_ms = int(time.time() * 1000)
        candles = []
        for item in payload or []:
            if len(item) < 5:
                continue
            open_time = float(item[0])
            close_time = float(item[6]) if len(item) > 6 else open_time
            candles.append(
                {
                    "open_time": open_time / 1000,
                    "close_time": close_time / 1000,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "base_volume": float(item[5] or 0) if len(item) > 5 else 0.0,
                    "quote_volume": float(item[7] or 0) if len(item) > 7 else 0.0,
                    "confirmed": close_time <= now_ms,
                }
            )
        return candles

    def _fetch_okx_candles(self, symbol: str, interval: str, mark: bool) -> list[dict]:
        path = (
            "/api/v5/market/history-mark-price-candles"
            if mark
            else "/api/v5/market/history-candles"
        )
        query = urlencode({"instId": _okx_inst_id(symbol), "bar": interval, "limit": "24"})
        request = Request(
            f"https://www.okx.com{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "crypto-futures-monitor/0.1",
            },
        )
        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("code")) != "0":
            raise ValueError(payload.get("msg") or "OKX candles failed")

        data = list(payload.get("data") or [])
        data.reverse()
        candles = []
        for item in data:
            if len(item) < 5:
                continue
            candles.append(
                {
                    "open_time": float(item[0]) / 1000,
                    "close_time": (float(item[0]) / 1000),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "base_volume": float(item[6] or 0) if len(item) > 6 else 0.0,
                    "quote_volume": float(item[7] or 0) if len(item) > 7 else 0.0,
                    "confirmed": str(item[-1]) == "1",
                }
            )
        return candles

    def _fetch_okx_candles_range(
        self,
        symbol: str,
        interval: str,
        event_time: float,
        target_time: float,
        interval_seconds: int,
        mark: bool,
    ) -> list[dict]:
        path = (
            "/api/v5/market/history-mark-price-candles"
            if mark
            else "/api/v5/market/history-candles"
        )
        limit = min(max(int((target_time - event_time) / interval_seconds) + 4, 24), 100)
        query = urlencode(
            {
                "instId": _okx_inst_id(symbol),
                "bar": interval,
                "limit": str(limit),
            }
        )
        request = Request(
            f"https://www.okx.com{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "crypto-futures-monitor/0.1",
            },
        )
        with urlopen(request, timeout=self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("code")) != "0":
            raise ValueError(payload.get("msg") or "OKX candles failed")

        data = list(payload.get("data") or [])
        data.reverse()
        candles = []
        for item in data:
            if len(item) < 5:
                continue
            open_time = float(item[0]) / 1000
            candles.append(
                {
                    "open_time": open_time,
                    "close_time": open_time + interval_seconds,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "base_volume": float(item[6] or 0) if len(item) > 6 else 0.0,
                    "quote_volume": float(item[7] or 0) if len(item) > 7 else 0.0,
                    "confirmed": str(item[-1]) == "1",
                }
            )
        return candles
