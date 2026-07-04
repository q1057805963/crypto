import threading
from dataclasses import asdict
from datetime import datetime
from time import time

from monitor.anomaly import AnomalyEvent, SymbolSnapshot


REVIEW_PERIODS = (
    (5, "5m"),
    (15, "15m"),
    (60, "1h"),
    (240, "4h"),
    (1440, "1d"),
)


def normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw_symbol in symbols:
        symbol = "".join(ch for ch in raw_symbol.upper().strip() if ch.isalnum())
        if not symbol:
            continue
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def _empty_symbol_snapshot(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "score": 0,
        "direction": "waiting",
        "price": None,
        "updated_at": None,
        "price_move_pct_1m": 0,
        "price_move_pct_5m": 0,
        "quote_volume_1m": 0,
        "volume_multiplier": 0,
        "taker_buy_ratio_1m": 0.5,
        "trade_count_1m": 0,
        "open_interest": 0,
        "oi_change_pct_5m": 0,
        "funding_rate": 0,
        "spread_bps": 0,
        "depth_imbalance": 0,
        "bid_depth_notional": 0,
        "ask_depth_notional": 0,
        "depth_drop_pct_1m": 0,
        "support_price": 0,
        "resistance_price": 0,
        "support_distance_pct": 0,
        "resistance_distance_pct": 0,
        "window_vwap": 0,
        "vwap_deviation_pct": 0,
        "range_position_pct": 50,
        "bid_wall_price": 0,
        "bid_wall_notional": 0,
        "ask_wall_price": 0,
        "ask_wall_notional": 0,
        "long_liquidation_quote_1m": 0,
        "short_liquidation_quote_1m": 0,
        "liquidation_total_quote_1m": 0,
        "liquidation_event_count_1m": 0,
        "liquidation_data_status": "unavailable",
        "microstructure_status": "unavailable",
        "depth_data_age_seconds": None,
        "last_liquidation_age_seconds": None,
        "price_series_5m": [],
        "volume_series_5m": [],
        "oi_series_5m": [],
        "risk_level": "低风险",
        "bias": "观察：暂无明确方向",
        "confidence": 0,
        "reasons": [],
        "suggestions": [],
    }


def _review_type_label(direction: str) -> str:
    mapping = {
        "up": "上涨报警",
        "down": "下跌报警",
        "mixed": "震荡报警",
    }
    return mapping.get(direction, "其他报警")


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _build_review_group(label: str, events: list[dict]) -> dict:
    period_rows = []
    resolved_total = 0
    for horizon_minutes, period_label in REVIEW_PERIODS:
        rows = []
        for event in events:
            for followup in event.get("followups", []) or []:
                if followup.get("status") != "resolved":
                    continue
                if int(followup.get("horizon_minutes") or 0) != horizon_minutes:
                    continue
                rows.append(followup)

        close_values = [float(row.get("close_bps") or 0) for row in rows]
        positive_count = sum(1 for value in close_values if value > 0)
        resolved_total += len(rows)
        period_rows.append(
            {
                "horizon_minutes": horizon_minutes,
                "label": period_label,
                "count": len(rows),
                "avg_close_bps": _average(close_values),
                "avg_max_up_bps": _average(
                    [float(row.get("max_up_bps") or 0) for row in rows]
                ),
                "avg_max_down_bps": _average(
                    [float(row.get("max_down_bps") or 0) for row in rows]
                ),
                "positive_rate_pct": (
                    round(positive_count / len(rows) * 100, 1)
                    if rows
                    else None
                ),
            }
        )

    return {
        "label": label,
        "alert_count": len(events),
        "resolved_count": resolved_total,
        "periods": period_rows,
    }


def build_review_stats(events: list[dict]) -> dict:
    groups = [_build_review_group("全部报警", events)]
    direction_order = ("up", "down", "mixed")
    for direction in direction_order:
        direction_events = [
            event for event in events
            if str(event.get("direction", "")).lower() == direction
        ]
        if direction_events:
            groups.append(_build_review_group(_review_type_label(direction), direction_events))

    return {
        "event_count": len(events),
        "resolved_count": groups[0]["resolved_count"] if groups else 0,
        "groups": [group for group in groups if group["resolved_count"] > 0],
    }


class DashboardState:
    def __init__(self, symbols: list[str], data_source: str, exchange: str = "binance_usdm") -> None:
        self._lock = threading.Lock()
        self._data_source = data_source
        self._exchange = exchange
        self._source_note = ""
        self._symbols = {
            symbol: _empty_symbol_snapshot(symbol)
            for symbol in normalize_symbols(symbols)
        }
        self._events: list[dict] = []

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            normalized = normalize_symbols(symbols)
            self._symbols = {
                symbol: self._symbols.get(symbol, _empty_symbol_snapshot(symbol))
                for symbol in normalized
            }

    def update_snapshot(self, snapshot: SymbolSnapshot) -> None:
        with self._lock:
            data = asdict(snapshot)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            self._symbols[snapshot.symbol] = data

    def set_events(self, events: list[dict]) -> None:
        with self._lock:
            self._events = events[:50]

    def add_event(self, event: AnomalyEvent) -> None:
        with self._lock:
            data = asdict(event)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            data["ai_summary"] = list(data.get("ai_summary", ()))
            data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._events.insert(0, data)
            self._events = self._events[:50]

    def get_symbol_data(self, symbol: str) -> dict | None:
        with self._lock:
            return self._symbols.get(symbol.upper())

    def set_source(self, *, exchange: str, data_source: str, note: str = "") -> None:
        with self._lock:
            self._exchange = exchange
            self._data_source = data_source
            self._source_note = note

    def clear_source_note(self) -> None:
        with self._lock:
            self._source_note = ""

    def as_payload(self, symbols_filter: list[str] | None = None) -> dict:
        with self._lock:
            symbols = list(self._symbols.values())
            if symbols_filter is not None:
                wanted = {symbol.upper() for symbol in symbols_filter}
                symbols = [symbol for symbol in symbols if symbol["symbol"].upper() in wanted]
            symbols.sort(key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
            events = list(self._events)
            if symbols_filter is not None:
                wanted = {symbol.upper() for symbol in symbols_filter}
                events = [event for event in events if str(event.get("symbol", "")).upper() in wanted]
            review_stats = build_review_stats(events)
            return {
                "generated_at": time(),
                "data_source": self._data_source,
                "exchange": self._exchange,
                "source_note": self._source_note,
                "symbols": symbols,
                "events": events,
                "review_stats": review_stats,
            }

    def get_source(self) -> dict:
        with self._lock:
            return {
                "exchange": self._exchange,
                "data_source": self._data_source,
            }
