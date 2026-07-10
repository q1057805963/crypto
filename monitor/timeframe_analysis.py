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
MULTI_TIMEFRAME_PERIODS = ("5m", "15m", "1h", "4h")

STRUCTURE_LOOKBACK_LIMIT = 96
VOLUME_PROFILE_BIN_COUNT = 28
VALUE_AREA_RATIO = 0.70

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


def _level_distance_pct(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 0.0
    return abs(price / level - 1) * 100


def _average_range_pct(candles: list[dict]) -> float:
    samples = []
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        if close > 0 and high >= low:
            samples.append((high - low) / close * 100)
    return mean(samples) if samples else 0.0


def _empty_volume_profile() -> dict:
    return {
        "profile_poc_price": 0.0,
        "profile_poc_quote_volume": 0.0,
        "value_area_low": 0.0,
        "value_area_high": 0.0,
        "support_profile_price": 0.0,
        "support_profile_quote_volume": 0.0,
        "resistance_profile_price": 0.0,
        "resistance_profile_quote_volume": 0.0,
        "profile_total_quote_volume": 0.0,
        "profile_bins": [],
    }


def _volume_profile(candles: list[dict], price: float, bin_count: int = VOLUME_PROFILE_BIN_COUNT) -> dict:
    if not candles:
        return _empty_volume_profile()

    period_low = min(float(item["low"]) for item in candles)
    period_high = max(float(item["high"]) for item in candles)
    if period_low <= 0 or period_high <= period_low:
        return _empty_volume_profile()

    bin_count = max(8, int(bin_count))
    step = (period_high - period_low) / bin_count
    bins = [
        {
            "low": period_low + index * step,
            "high": period_low + (index + 1) * step,
            "price": period_low + (index + 0.5) * step,
            "quote_volume": 0.0,
        }
        for index in range(bin_count)
    ]

    for candle in candles:
        low = float(candle["low"])
        high = float(candle["high"])
        close = float(candle["close"])
        quote_volume = float(candle.get("quote_volume") or 0)
        if quote_volume <= 0:
            continue
        if high <= low:
            index = min(max(int((close - period_low) / step), 0), bin_count - 1)
            bins[index]["quote_volume"] += quote_volume
            continue

        span = high - low
        allocated = 0.0
        for item in bins:
            overlap = max(0.0, min(high, item["high"]) - max(low, item["low"]))
            if overlap <= 0:
                continue
            value = quote_volume * (overlap / span)
            item["quote_volume"] += value
            allocated += value
        if allocated <= 0:
            index = min(max(int((close - period_low) / step), 0), bin_count - 1)
            bins[index]["quote_volume"] += quote_volume

    total_volume = sum(float(item["quote_volume"]) for item in bins)
    if total_volume <= 0:
        return _empty_volume_profile()

    poc_index, poc = max(enumerate(bins), key=lambda item: float(item[1]["quote_volume"]))
    selected = {poc_index}
    selected_volume = float(poc["quote_volume"])
    left = poc_index - 1
    right = poc_index + 1
    target = total_volume * VALUE_AREA_RATIO
    while selected_volume < target and (left >= 0 or right < len(bins)):
        left_volume = float(bins[left]["quote_volume"]) if left >= 0 else -1.0
        right_volume = float(bins[right]["quote_volume"]) if right < len(bins) else -1.0
        if right_volume > left_volume:
            selected.add(right)
            selected_volume += max(right_volume, 0.0)
            right += 1
        else:
            selected.add(left)
            selected_volume += max(left_volume, 0.0)
            left -= 1

    def node_score(item: dict) -> float:
        distance = _level_distance_pct(price, float(item["price"]))
        share = float(item["quote_volume"]) / total_volume
        return share * 100 - distance * 1.8

    support_nodes = [item for item in bins if float(item["price"]) <= price and float(item["quote_volume"]) > 0]
    resistance_nodes = [item for item in bins if float(item["price"]) >= price and float(item["quote_volume"]) > 0]
    support_node = max(support_nodes, key=node_score) if support_nodes else None
    resistance_node = max(resistance_nodes, key=node_score) if resistance_nodes else None
    top_nodes = sorted(bins, key=lambda item: float(item["quote_volume"]), reverse=True)[:8]

    return {
        "profile_poc_price": float(poc["price"]),
        "profile_poc_quote_volume": float(poc["quote_volume"]),
        "value_area_low": min(float(bins[index]["low"]) for index in selected),
        "value_area_high": max(float(bins[index]["high"]) for index in selected),
        "support_profile_price": float(support_node["price"]) if support_node else 0.0,
        "support_profile_quote_volume": float(support_node["quote_volume"]) if support_node else 0.0,
        "resistance_profile_price": float(resistance_node["price"]) if resistance_node else 0.0,
        "resistance_profile_quote_volume": float(resistance_node["quote_volume"]) if resistance_node else 0.0,
        "profile_total_quote_volume": total_volume,
        "profile_bins": [
            {
                "price": float(item["price"]),
                "quote_volume": float(item["quote_volume"]),
                "share_pct": (float(item["quote_volume"]) / total_volume) * 100,
            }
            for item in top_nodes
            if float(item["quote_volume"]) > 0
        ],
    }


def _is_pivot(candles: list[dict], index: int, key: str, low: bool) -> bool:
    left = candles[max(0, index - 2):index]
    right = candles[index + 1:index + 3]
    if not left or not right:
        return False
    value = float(candles[index][key])
    neighbors = [float(item[key]) for item in [*left, *right]]
    return value <= min(neighbors) if low else value >= max(neighbors)


def _cluster_levels(candidates: list[dict], tolerance_pct: float) -> list[dict]:
    if not candidates:
        return []

    clusters = []
    for candidate in sorted(candidates, key=lambda item: item["level"]):
        level = float(candidate["level"])
        matched = None
        for cluster in clusters:
            if _level_distance_pct(float(cluster["anchor_level"]), level) <= tolerance_pct:
                matched = cluster
                break
        if matched is None:
            matched = {
                "anchor_level": level,
                "levels": [],
                "weights": [],
                "pivot_count": 0,
                "touch_count": 0,
                "quote_volume": 0.0,
                "source_counts": {},
                "latest_index": 0,
                "open_time": candidate["open_time"],
            }
            clusters.append(matched)
        weight = max(float(candidate.get("quote_volume") or 0), 1.0)
        source = str(candidate.get("source") or "touch")
        matched["levels"].append(level)
        matched["weights"].append(weight)
        weight_sum = sum(matched["weights"]) or 1.0
        matched["anchor_level"] = sum(
            item_level * item_weight
            for item_level, item_weight in zip(matched["levels"], matched["weights"])
        ) / weight_sum
        matched["touch_count"] += 1
        matched["pivot_count"] += 1 if candidate.get("pivot") else 0
        matched["quote_volume"] += float(candidate.get("quote_volume") or 0)
        matched["source_counts"][source] = int(matched["source_counts"].get(source, 0)) + 1
        if int(candidate["index"]) >= int(matched["latest_index"]):
            matched["latest_index"] = int(candidate["index"])
            matched["open_time"] = candidate["open_time"]

    output = []
    for cluster in clusters:
        weight_sum = sum(cluster["weights"]) or 1.0
        output.append(
            {
                "level": sum(level * weight for level, weight in zip(cluster["levels"], cluster["weights"])) / weight_sum,
                "pivot_count": int(cluster["pivot_count"]),
                "touch_count": int(cluster["touch_count"]),
                "quote_volume": float(cluster["quote_volume"]),
                "source_counts": dict(cluster["source_counts"]),
                "volume_profile_count": int(cluster["source_counts"].get("volume_profile", 0)),
                "latest_index": int(cluster["latest_index"]),
                "open_time": float(cluster["open_time"]),
            }
        )
    return output


def _select_structure_level(
    candles: list[dict],
    price: float,
    support: bool,
    volume_profile: dict | None = None,
) -> dict:
    key = "low" if support else "high"
    fallback = (
        min(candles, key=lambda item: float(item["low"]))
        if support
        else max(candles, key=lambda item: float(item["high"]))
    )
    fallback_source = "range_low" if support else "range_high"
    if len(candles) < 6 or price <= 0:
        return {
            "price": float(fallback[key]),
            "open_time": float(fallback["open_time"]),
            "source": fallback_source,
            "touch_count": 1,
            "pivot_count": 0,
            "strength": 0.0,
            "strength_score": 0.0,
            "tolerance_pct": 0.0,
        }

    candidates = []
    for index, candle in enumerate(candles):
        pivot = _is_pivot(candles, index, key, low=support)
        candidates.append(
            {
                "level": float(candle[key]),
                "quote_volume": float(candle.get("quote_volume") or 0),
                "open_time": float(candle["open_time"]),
                "index": index,
                "pivot": pivot,
                "source": "pivot" if pivot else "touch",
            }
        )

    tolerance_pct = min(max(_average_range_pct(candles) * 0.45, 0.12), 0.9)
    if volume_profile:
        profile_nodes = volume_profile.get("profile_bins") or []
        for node in profile_nodes:
            level = float(node.get("price") or 0)
            if level <= 0:
                continue
            if support and level > price:
                continue
            if not support and level < price:
                continue
            candidates.append(
                {
                    "level": level,
                    "quote_volume": float(node.get("quote_volume") or 0),
                    "open_time": float(candles[-1]["open_time"]),
                    "index": len(candles) - 1,
                    "pivot": False,
                    "source": "volume_profile",
                }
            )

    clusters = _cluster_levels(candidates, tolerance_pct)
    if support:
        directional = [cluster for cluster in clusters if float(cluster["level"]) <= price]
    else:
        directional = [cluster for cluster in clusters if float(cluster["level"]) >= price]

    if not directional:
        return {
            "price": float(fallback[key]),
            "open_time": float(fallback["open_time"]),
            "source": fallback_source,
            "touch_count": 1,
            "pivot_count": 0,
            "strength": 0.0,
            "strength_score": 0.0,
            "tolerance_pct": round(tolerance_pct, 3),
        }

    max_volume = max((float(cluster["quote_volume"]) for cluster in directional), default=1.0) or 1.0
    last_index = max(len(candles) - 1, 1)

    def score(cluster: dict) -> float:
        distance = _level_distance_pct(price, float(cluster["level"]))
        recency = float(cluster["latest_index"]) / last_index
        volume_score = float(cluster["quote_volume"]) / max_volume
        distance_score = max(0.0, 3.0 - distance * 0.35)
        profile_score = 1.9 if int(cluster.get("volume_profile_count") or 0) > 0 else 0.0
        return (
            float(cluster["touch_count"]) * 1.7
            + float(cluster["pivot_count"]) * 2.2
            + volume_score * 1.4
            + recency * 1.1
            + distance_score
            + profile_score
        )

    selected = max(directional, key=score)
    raw_strength = score(selected)
    has_profile = int(selected.get("volume_profile_count") or 0) > 0
    if int(selected["pivot_count"]) > 0 and has_profile:
        source = "swing_volume_cluster"
    elif has_profile:
        source = "volume_profile_cluster"
    elif int(selected["pivot_count"]) > 0:
        source = "swing_cluster"
    else:
        source = "touch_cluster"
    return {
        "price": float(selected["level"]),
        "open_time": float(selected["open_time"]),
        "source": source,
        "touch_count": int(selected["touch_count"]),
        "pivot_count": int(selected["pivot_count"]),
        "strength": round(raw_strength, 2),
        "strength_score": round(min(raw_strength * 7.5, 100.0), 1),
        "tolerance_pct": round(tolerance_pct, 3),
    }


def _near_level(price: float, level: float, tolerance_pct: float) -> bool:
    if price <= 0 or level <= 0:
        return False
    return _level_distance_pct(price, level) <= tolerance_pct


def _level_confluence_score(
    *,
    level: float,
    level_data: dict,
    volume_profile: dict,
    window_vwap: float,
    support: bool,
) -> float:
    score = 0.0
    score += min(int(level_data.get("touch_count") or 0), 5) * 7.0
    score += min(int(level_data.get("pivot_count") or 0), 3) * 10.0
    score += min(float(level_data.get("strength") or 0) * 2.2, 22.0)
    if "volume" in str(level_data.get("source") or ""):
        score += 18.0

    tolerance_pct = max(float(level_data.get("tolerance_pct") or 0.12), 0.12)
    if _near_level(level, float(volume_profile.get("profile_poc_price") or 0), tolerance_pct * 1.4):
        score += 14.0

    value_edge = (
        float(volume_profile.get("value_area_low") or 0)
        if support
        else float(volume_profile.get("value_area_high") or 0)
    )
    if _near_level(level, value_edge, tolerance_pct * 1.2):
        score += 10.0

    profile_edge = (
        float(volume_profile.get("support_profile_price") or 0)
        if support
        else float(volume_profile.get("resistance_profile_price") or 0)
    )
    if _near_level(level, profile_edge, tolerance_pct * 1.2):
        score += 10.0

    if _near_level(level, window_vwap, tolerance_pct * 1.5):
        score += 7.0

    return round(min(score, 100.0), 1)


def _level_status(
    *,
    price: float,
    level: float,
    support: bool,
    tolerance_pct: float,
    price_move_pct: float,
    volume_multiplier: float,
) -> str:
    if price <= 0 or level <= 0:
        return "unknown"

    break_buffer = max(tolerance_pct * 0.45, 0.08)
    test_buffer = max(tolerance_pct * 0.75, 0.12)
    distance = _level_distance_pct(price, level)

    if support:
        if price < level * (1 - break_buffer / 100):
            return "lost_confirmed" if volume_multiplier >= 1.25 else "lost_soft"
        if distance <= test_buffer:
            return "testing"
        if price > level and distance <= test_buffer * 2.2 and price_move_pct > 0:
            return "bounce_watch"
        return "valid"

    if price > level * (1 + break_buffer / 100):
        return "breakout_confirmed" if volume_multiplier >= 1.25 else "breakout_soft"
    if distance <= test_buffer:
        return "testing"
    if price < level and distance <= test_buffer * 2.2 and price_move_pct < 0:
        return "rejection_watch"
    return "valid"


def _structure_regime(
    *,
    price: float,
    support: float,
    resistance: float,
    window_vwap: float,
    volume_profile: dict,
    support_status: str,
    resistance_status: str,
    range_position_pct: float,
) -> str:
    if support_status.startswith("lost"):
        return "support_lost"
    if resistance_status.startswith("breakout"):
        return "resistance_breakout"
    if support_status in {"testing", "bounce_watch"}:
        return "support_test"
    if resistance_status in {"testing", "rejection_watch"}:
        return "resistance_test"

    value_low = float(volume_profile.get("value_area_low") or 0)
    value_high = float(volume_profile.get("value_area_high") or 0)
    if value_low > 0 and value_high > value_low and value_low <= price <= value_high:
        return "value_area_rotation"
    if window_vwap > 0 and price > window_vwap and range_position_pct >= 65:
        return "upper_acceptance"
    if window_vwap > 0 and price < window_vwap and range_position_pct <= 35:
        return "lower_acceptance"
    return "balanced"


def _normalized_exchange(exchange: str) -> str:
    value = str(exchange or "").strip().lower()
    return "okx" if value.startswith("okx") else "binance"


def _okx_inst_id(symbol: str) -> str:
    symbol = symbol.upper()
    if "-" in symbol:
        return symbol
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}-USDT-SWAP"


def _okx_interval_seconds(interval: str) -> int:
    for config in TIMEFRAME_CONFIG.values():
        if config["okx"] == interval:
            return int(config["seconds"])
    return 0


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

    candles = price_candles[-STRUCTURE_LOOKBACK_LIMIT:]
    current = candles[-1]
    previous = next((item for item in reversed(candles[:-1]) if item.get("confirmed")), None)
    if previous is None and len(candles) >= 2:
        previous = candles[-2]
    previous = previous or current

    support_candle = min(candles, key=lambda item: float(item["low"]))
    resistance_candle = max(candles, key=lambda item: float(item["high"]))
    period_low_price = float(support_candle["low"])
    period_high_price = float(resistance_candle["high"])
    close_price = float(current["close"])
    volume_profile = _volume_profile(candles, close_price)
    support_level = _select_structure_level(
        candles,
        close_price,
        support=True,
        volume_profile=volume_profile,
    )
    resistance_level = _select_structure_level(
        candles,
        close_price,
        support=False,
        volume_profile=volume_profile,
    )
    support_price = float(support_level["price"])
    resistance_price = float(resistance_level["price"])
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
    support_confluence_score = _level_confluence_score(
        level=support_price,
        level_data=support_level,
        volume_profile=volume_profile,
        window_vwap=window_vwap,
        support=True,
    )
    resistance_confluence_score = _level_confluence_score(
        level=resistance_price,
        level_data=resistance_level,
        volume_profile=volume_profile,
        window_vwap=window_vwap,
        support=False,
    )
    support_status = _level_status(
        price=close_price,
        level=support_price,
        support=True,
        tolerance_pct=max(float(support_level.get("tolerance_pct") or 0.12), 0.12),
        price_move_pct=price_move_pct,
        volume_multiplier=volume_multiplier,
    )
    resistance_status = _level_status(
        price=close_price,
        level=resistance_price,
        support=False,
        tolerance_pct=max(float(resistance_level.get("tolerance_pct") or 0.12), 0.12),
        price_move_pct=price_move_pct,
        volume_multiplier=volume_multiplier,
    )
    structure_regime = _structure_regime(
        price=close_price,
        support=support_price,
        resistance=resistance_price,
        window_vwap=window_vwap,
        volume_profile=volume_profile,
        support_status=support_status,
        resistance_status=resistance_status,
        range_position_pct=range_position_pct,
    )

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
        "period_low_price": round(period_low_price, 8),
        "period_high_price": round(period_high_price, 8),
        "window_vwap": round(window_vwap, 8),
        "vwap_deviation_pct": round(vwap_deviation_pct, 3),
        "support_distance_pct": round(support_distance_pct, 3),
        "resistance_distance_pct": round(resistance_distance_pct, 3),
        "range_position_pct": round(range_position_pct, 2),
        "price_series": [round(float(item["close"]), 8) for item in candles],
        "open_series": [round(float(item["open"]), 8) for item in candles],
        "low_series": [round(float(item["low"]), 8) for item in candles],
        "high_series": [round(float(item["high"]), 8) for item in candles],
        "time_series": [int(item["open_time"]) for item in candles],
        "volume_series": [round(float(item.get("quote_volume") or 0), 2) for item in candles],
        "support_open_time": float(support_level["open_time"]),
        "resistance_open_time": float(resistance_level["open_time"]),
        "period_low_open_time": float(support_candle["open_time"]),
        "period_high_open_time": float(resistance_candle["open_time"]),
        "support_source": support_level["source"],
        "resistance_source": resistance_level["source"],
        "support_touch_count": support_level["touch_count"],
        "resistance_touch_count": resistance_level["touch_count"],
        "support_pivot_count": support_level["pivot_count"],
        "resistance_pivot_count": resistance_level["pivot_count"],
        "support_strength": support_level["strength"],
        "resistance_strength": resistance_level["strength"],
        "support_strength_score": support_level["strength_score"],
        "resistance_strength_score": resistance_level["strength_score"],
        "support_confluence_score": support_confluence_score,
        "resistance_confluence_score": resistance_confluence_score,
        "support_status": support_status,
        "resistance_status": resistance_status,
        "structure_regime": structure_regime,
        "structure_tolerance_pct": max(support_level["tolerance_pct"], resistance_level["tolerance_pct"]),
        "structure_sample_count": len(candles),
        "profile_poc_price": round(float(volume_profile["profile_poc_price"]), 8),
        "profile_poc_quote_volume": round(float(volume_profile["profile_poc_quote_volume"]), 2),
        "value_area_low": round(float(volume_profile["value_area_low"]), 8),
        "value_area_high": round(float(volume_profile["value_area_high"]), 8),
        "support_profile_price": round(float(volume_profile["support_profile_price"]), 8),
        "support_profile_quote_volume": round(float(volume_profile["support_profile_quote_volume"]), 2),
        "resistance_profile_price": round(float(volume_profile["resistance_profile_price"]), 8),
        "resistance_profile_quote_volume": round(float(volume_profile["resistance_profile_quote_volume"]), 2),
        "profile_total_quote_volume": round(float(volume_profile["profile_total_quote_volume"]), 2),
        "mark_price": round(mark_price, 8) if mark_price is not None else None,
        "mark_move_pct": round(mark_move_pct, 3) if mark_move_pct is not None else None,
        "mark_prev_close_pct": round(mark_prev_close_pct, 3) if mark_prev_close_pct is not None else None,
        "mark_premium_bps": round(mark_premium_bps, 3) if mark_premium_bps is not None else None,
        "mark_price_series": (
            [round(float(item["close"]), 8) for item in mark_candles[-STRUCTURE_LOOKBACK_LIMIT:]]
            if mark_candles
            else []
        ),
    }


def _period_weight(period: str) -> float:
    return {
        "5m": 1.0,
        "15m": 1.25,
        "1h": 1.65,
        "4h": 2.1,
    }.get(str(period), 1.0)


def _regime_label(regime: str) -> str:
    return {
        "support_lost": "支撑失守",
        "resistance_breakout": "压力突破",
        "support_test": "测试支撑",
        "resistance_test": "测试压力",
        "value_area_rotation": "价值区轮动",
        "upper_acceptance": "上半区接受",
        "lower_acceptance": "下半区接受",
        "balanced": "均衡",
    }.get(str(regime), str(regime or "未知"))


def _period_bias_score(data: dict) -> float:
    regime = str(data.get("structure_regime") or "")
    move = float(data.get("price_move_pct") or 0)
    volume_multiplier = float(data.get("volume_multiplier") or 0)
    vwap_deviation = float(data.get("vwap_deviation_pct") or 0)
    range_position = float(data.get("range_position_pct") or 50)
    score = 0.0

    if regime == "resistance_breakout":
        score += 0.72
    elif regime == "support_lost":
        score -= 0.72
    elif regime == "upper_acceptance":
        score += 0.45
    elif regime == "lower_acceptance":
        score -= 0.45
    elif regime == "support_test":
        score += 0.22
    elif regime == "resistance_test":
        score -= 0.22

    if move >= 0.8:
        score += 0.28
    elif move <= -0.8:
        score -= 0.28
    elif move >= 0.25:
        score += 0.12
    elif move <= -0.25:
        score -= 0.12

    if vwap_deviation >= 0.15:
        score += 0.18
    elif vwap_deviation <= -0.15:
        score -= 0.18

    if range_position >= 70:
        score += 0.12
    elif range_position <= 30:
        score -= 0.12

    if volume_multiplier >= 1.5:
        score *= 1.15
    elif volume_multiplier > 0 and volume_multiplier <= 0.75:
        score *= 0.78

    return max(-1.0, min(1.0, score))


def _bias_label(score: float) -> str:
    if score >= 0.22:
        return "up"
    if score <= -0.22:
        return "down"
    return "mixed"


def build_multi_timeframe_confluence(
    *,
    symbol: str,
    exchange: str,
    analyses: list[dict],
) -> dict:
    ordered = [
        item for item in sorted(
            analyses,
            key=lambda data: MULTI_TIMEFRAME_PERIODS.index(str(data.get("period")))
            if str(data.get("period")) in MULTI_TIMEFRAME_PERIODS
            else 99,
        )
        if str(item.get("period")) in MULTI_TIMEFRAME_PERIODS
    ]
    if not ordered:
        raise ValueError("no timeframe analyses")

    period_rows = []
    weighted_sum = 0.0
    total_weight = 0.0
    for item in ordered:
        period = str(item.get("period") or "")
        raw_score = _period_bias_score(item)
        weight = _period_weight(period)
        total_weight += weight
        weighted_sum += raw_score * weight
        bias = _bias_label(raw_score)
        period_rows.append(
            {
                "period": period,
                "period_label": item.get("period_label") or period,
                "bias": bias,
                "bias_score": round(raw_score * 100, 1),
                "weight": weight,
                "structure_regime": item.get("structure_regime") or "unknown",
                "structure_label": _regime_label(str(item.get("structure_regime") or "")),
                "price_move_pct": round(float(item.get("price_move_pct") or 0), 3),
                "volume_multiplier": round(float(item.get("volume_multiplier") or 0), 2),
                "vwap_deviation_pct": round(float(item.get("vwap_deviation_pct") or 0), 3),
                "range_position_pct": round(float(item.get("range_position_pct") or 50), 2),
                "support_price": item.get("support_price"),
                "resistance_price": item.get("resistance_price"),
            }
        )

    net = weighted_sum / total_weight if total_weight > 0 else 0.0
    direction = _bias_label(net)
    aligned = [
        item for item in period_rows
        if direction in {"up", "down"} and item["bias"] == direction
    ]
    conflicts = [
        item for item in period_rows
        if direction in {"up", "down"} and item["bias"] in {"up", "down"} and item["bias"] != direction
    ]
    alignment_ratio = len(aligned) / len(period_rows) if period_rows else 0.0
    score = min(100.0, abs(net) * 72.0 + alignment_ratio * 28.0)

    if direction == "up":
        label = "多周期偏多共振" if score >= 65 else "多周期偏多但需确认"
    elif direction == "down":
        label = "多周期偏空共振" if score >= 65 else "多周期偏空但需确认"
    else:
        label = "多周期分歧"

    confirmations = [
        f"{item['period_label']} {_regime_label(item['structure_regime'])}，涨跌 {item['price_move_pct']:+.2f}%"
        for item in aligned[:3]
    ]
    conflict_text = [
        f"{item['period_label']} {_regime_label(item['structure_regime'])}，与主方向冲突"
        for item in conflicts[:3]
    ]
    if direction == "mixed":
        conflict_text = [
            f"{item['period_label']} {_regime_label(item['structure_regime'])}，方向不统一"
            for item in period_rows[:4]
        ]

    summary = (
        f"{label}，共振分 {score:.1f}，"
        f"{len(aligned)}/{len(period_rows)} 个周期同向。"
    )
    if conflicts:
        summary += f" 主要分歧来自 {', '.join(item['period_label'] for item in conflicts[:2])}。"

    return {
        "symbol": symbol.upper(),
        "exchange": exchange,
        "generated_at": time.time(),
        "periods": period_rows,
        "direction": direction,
        "score": round(score, 1),
        "label": label,
        "summary": summary,
        "confirmations": confirmations,
        "conflicts": conflict_text,
        "sample_count": len(period_rows),
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

    def confluence(
        self,
        symbol: str,
        exchange: str,
        force: bool = False,
        periods: tuple[str, ...] = MULTI_TIMEFRAME_PERIODS,
    ) -> dict:
        analyses = []
        errors = []
        for period in periods:
            try:
                analyses.append(self.analyze(symbol, period, exchange, force=force))
            except Exception as exc:
                errors.append({"period": period, "error": str(exc)})
        if not analyses:
            raise ValueError("no timeframe analyses")
        result = build_multi_timeframe_confluence(
            symbol=symbol,
            exchange=exchange,
            analyses=analyses,
        )
        if errors:
            result["errors"] = errors
        return result

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
        query = urlencode({"symbol": symbol.upper(), "interval": interval, "limit": str(STRUCTURE_LOOKBACK_LIMIT)})
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
            "/api/v5/market/mark-price-candles"
            if mark
            else "/api/v5/market/candles"
        )
        query = urlencode({"instId": _okx_inst_id(symbol), "bar": interval, "limit": str(STRUCTURE_LOOKBACK_LIMIT)})
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
        interval_seconds = _okx_interval_seconds(interval)
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
