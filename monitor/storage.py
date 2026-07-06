import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import time

from monitor.anomaly import AnomalyEvent, SymbolSnapshot


FOLLOWUP_HORIZONS_MINUTES = (5, 15, 60, 240, 1440)
STATS_HORIZON_MINUTES = 15

TRIGGER_COMPONENT_LABELS = {
    "price_1m": "1m价格冲击",
    "price_5m": "5m趋势冲击",
    "volume": "放量",
    "taker_buy": "主动买入",
    "taker_sell": "主动卖出",
    "oi_up": "持仓增加",
    "oi_down": "持仓下降",
    "funding_positive": "多头拥挤",
    "funding_negative": "空头拥挤",
    "long_liquidation": "多头爆仓",
    "short_liquidation": "空头爆仓",
    "spread": "点差扩大",
    "bid_depth": "买盘深度占优",
    "ask_depth": "卖盘深度占优",
    "depth_drop": "深度下降",
    "price_volume": "价量共振",
    "taker_aligned": "主动成交同向",
    "oi_aligned": "价仓同向",
    "liquidation_aligned": "爆仓同向",
    "liquidity_risk": "流动性变薄",
    "unclassified": "未分类触发",
}


def _has_reason(data: dict, text: str) -> bool:
    return any(text in str(reason) for reason in data.get("reasons", []) or [])


def trigger_combo(data: dict) -> dict:
    direction = str(data.get("direction") or "")
    components: list[str] = []

    def add(component: str) -> None:
        if component not in components:
            components.append(component)

    if _has_reason(data, "1分钟价格波动") or abs(float(data.get("price_move_pct_1m") or 0)) >= 0.6:
        add("price_1m")
    if _has_reason(data, "5分钟价格波动") or abs(float(data.get("price_move_pct_5m") or 0)) >= 1.2:
        add("price_5m")
    if _has_reason(data, "成交额放大") or float(data.get("volume_multiplier") or 0) >= 2.2:
        add("volume")

    taker_buy_ratio = float(data.get("taker_buy_ratio_1m") or data.get("taker_buy_ratio") or 0.5)
    if _has_reason(data, "主动买入") or taker_buy_ratio >= 0.68:
        add("taker_buy")
    if _has_reason(data, "主动卖出") or taker_buy_ratio <= 0.32:
        add("taker_sell")

    oi_change = float(data.get("oi_change_pct_5m") or 0)
    if _has_reason(data, "持仓量变化") or abs(oi_change) >= 0.8:
        add("oi_up" if oi_change >= 0 else "oi_down")

    funding_rate = float(data.get("funding_rate") or 0)
    if _has_reason(data, "资金费率") or abs(funding_rate) >= 0.0003:
        add("funding_positive" if funding_rate >= 0 else "funding_negative")

    long_liq = float(data.get("long_liquidation_quote_1m") or 0)
    short_liq = float(data.get("short_liquidation_quote_1m") or 0)
    liq_total = float(data.get("liquidation_total_quote_1m") or (long_liq + short_liq))
    if _has_reason(data, "多头爆仓") or (liq_total >= 75000 and long_liq >= short_liq):
        add("long_liquidation")
    if _has_reason(data, "空头爆仓") or (liq_total >= 75000 and short_liq > long_liq):
        add("short_liquidation")

    if _has_reason(data, "盘口点差") or float(data.get("spread_bps") or 0) >= 3.0:
        add("spread")

    depth_imbalance = float(data.get("depth_imbalance") or 0)
    if _has_reason(data, "买盘深度") or depth_imbalance >= 0.22:
        add("bid_depth")
    if _has_reason(data, "卖盘深度") or depth_imbalance <= -0.22:
        add("ask_depth")
    if _has_reason(data, "盘口深度下降") or float(data.get("depth_drop_pct_1m") or 0) >= 15:
        add("depth_drop")

    if _has_reason(data, "价格与放量共振"):
        add("price_volume")
    if _has_reason(data, "主动成交与价格方向一致"):
        add("taker_aligned")
    if _has_reason(data, "价格与持仓同向增加"):
        add("oi_aligned")
    if _has_reason(data, "爆仓方向与价格推动一致"):
        add("liquidation_aligned")
    if _has_reason(data, "流动性变薄"):
        add("liquidity_risk")

    if not components:
        add("unclassified")

    labels = [TRIGGER_COMPONENT_LABELS.get(component, component) for component in components]
    return {
        "key": "+".join(components),
        "direction": direction,
        "components": components,
        "labels": labels,
        "label": " + ".join(labels),
    }


def _bps_change(anchor_price: float, target_price: float) -> float:
    if anchor_price <= 0 or target_price <= 0:
        return 0.0
    return (target_price / anchor_price - 1) * 10000


def _directional_bps(direction: str, bps: float | None) -> float:
    value = float(bps or 0)
    if direction == "down":
        return -value
    if direction == "up":
        return value
    return 0.0


def _directional_followup(direction: str, item: dict) -> dict:
    if direction not in {"up", "down"} or item.get("status") != "resolved":
        return {}

    close_directional_bps = _directional_bps(direction, item.get("close_bps"))
    if direction == "up":
        max_favorable_bps = max(float(item.get("max_up_bps") or 0), 0.0)
        max_adverse_bps = max(abs(float(item.get("max_down_bps") or 0)), 0.0)
    else:
        max_favorable_bps = max(abs(float(item.get("max_down_bps") or 0)), 0.0)
        max_adverse_bps = max(float(item.get("max_up_bps") or 0), 0.0)

    if close_directional_bps >= 20 and max_favorable_bps >= max_adverse_bps * 0.8:
        verdict = "validated"
    elif close_directional_bps <= -20 and max_adverse_bps > max_favorable_bps:
        verdict = "failed"
    elif max_favorable_bps >= 40 and close_directional_bps < 0:
        verdict = "faded"
    else:
        verdict = "neutral"

    return {
        "directional_close_bps": round(close_directional_bps, 3),
        "max_favorable_bps": round(max_favorable_bps, 3),
        "max_adverse_bps": round(max_adverse_bps, 3),
        "verdict": verdict,
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


def _snapshot_value(snapshot_data: dict | None, key: str, default: float = 0.0) -> float:
    if not snapshot_data:
        return default
    try:
        return float(snapshot_data.get(key) or default)
    except (TypeError, ValueError):
        return default


def _snapshot_series(snapshot_data: dict | None, key: str) -> list[float]:
    if not snapshot_data:
        return []
    raw = snapshot_data.get(key) or []
    if not isinstance(raw, (list, tuple)):
        return []
    values = []
    for item in raw:
        try:
            value = float(item or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return values


def _local_volatility_bps(snapshot_data: dict | None) -> float:
    prices = _snapshot_series(snapshot_data, "price_series_5m")
    if len(prices) < 2:
        return 0.0
    step_changes = [
        abs(_bps_change(prices[index - 1], prices[index]))
        for index in range(1, len(prices))
        if prices[index - 1] > 0 and prices[index] > 0
    ]
    range_bps = abs(_bps_change(min(prices), max(prices))) if min(prices) > 0 else 0.0
    avg_step_bps = sum(step_changes) / len(step_changes) if step_changes else 0.0
    return max(avg_step_bps * 2.0, range_bps * 0.25)


def _estimated_tick_bps(price: float) -> float:
    if price <= 0:
        return 0.0
    if price >= 100000:
        tick = 1.0
    elif price >= 10000:
        tick = 0.1
    elif price >= 1000:
        tick = 0.01
    elif price >= 100:
        tick = 0.001
    elif price >= 1:
        tick = 0.0001
    else:
        tick = max(price * 0.0001, 0.00000001)
    return max((tick / price) * 10000 * 4, 0.8)


def _level_candidates(snapshot_data: dict | None, specs: list[tuple[str, str]]) -> list[dict]:
    candidates = []
    for key, label in specs:
        value = _snapshot_value(snapshot_data, key)
        if value > 0:
            candidates.append({"price": value, "label": label, "key": key})
    return candidates


def _choose_level(candidates: list[dict], price: float, *, above: bool) -> dict | None:
    if above:
        valid = [item for item in candidates if float(item["price"]) > price]
        return min(valid, key=lambda item: float(item["price"]) - price) if valid else None
    valid = [item for item in candidates if 0 < float(item["price"]) < price]
    return max(valid, key=lambda item: float(item["price"])) if valid else None


def _decision_metrics(event: AnomalyEvent, snapshot_data: dict | None = None) -> dict:
    direction = str(event.direction or "")
    price = float(event.price or 0)
    if direction not in {"up", "down"} or price <= 0:
        return {
            "directional": False,
            "reason": "方向未确认，暂不生成失效价。",
        }

    support = _snapshot_value(snapshot_data, "support_price")
    resistance = _snapshot_value(snapshot_data, "resistance_price")
    spread_bps = max(float(event.spread_bps or 0), _snapshot_value(snapshot_data, "spread_bps"))
    depth_drop_pct = max(float(event.depth_drop_pct_1m or 0), _snapshot_value(snapshot_data, "depth_drop_pct_1m"))
    depth_imbalance = max(abs(float(event.depth_imbalance or 0)), abs(_snapshot_value(snapshot_data, "depth_imbalance")))
    bid_depth = _snapshot_value(snapshot_data, "bid_depth_notional", float(event.bid_depth_notional or 0))
    ask_depth = _snapshot_value(snapshot_data, "ask_depth_notional", float(event.ask_depth_notional or 0))
    depth_total = bid_depth + ask_depth
    quote_volume = max(float(event.quote_volume_1m or 0), _snapshot_value(snapshot_data, "quote_volume_1m"))
    mark_premium_bps = max(
        abs(float(getattr(event, "mark_premium_bps", 0) or 0)),
        abs(_snapshot_value(snapshot_data, "mark_premium_bps")),
    )
    range_bps = abs(_bps_change(support, resistance)) if support > 0 and resistance > 0 else 0.0
    impulse_bps = abs(float(event.price_move_pct_1m or 0)) * 100
    local_volatility_bps = _local_volatility_bps(snapshot_data)
    tick_bps = _estimated_tick_bps(price)
    depth_ratio = (depth_total / quote_volume) if depth_total > 0 and quote_volume > 0 else 0.0
    depth_ratio_bps = 0.0
    if 0 < depth_ratio < 0.25:
        depth_ratio_bps = 28.0
    elif 0 < depth_ratio < 0.5:
        depth_ratio_bps = 18.0
    elif 0 < depth_ratio < 1.0:
        depth_ratio_bps = 9.0
    liquidity_buffer_bps = min(
        spread_bps * 1.8
        + depth_drop_pct * 0.32
        + depth_imbalance * 10.0
        + depth_ratio_bps * 0.45,
        45.0,
    )
    buffer_bps = min(
        max(14.0, tick_bps, spread_bps * 2.5, range_bps * 0.06, impulse_bps * 0.12, local_volatility_bps * 0.85, mark_premium_bps * 0.75)
        + liquidity_buffer_bps,
        160.0,
    )
    buffer_components = {
        "tick_bps": round(tick_bps, 3),
        "spread_bps": round(spread_bps, 3),
        "range_bps": round(range_bps, 3),
        "impulse_bps": round(impulse_bps, 3),
        "local_volatility_bps": round(local_volatility_bps, 3),
        "mark_premium_bps": round(mark_premium_bps, 3),
        "liquidity_buffer_bps": round(liquidity_buffer_bps, 3),
    }

    support_candidates = _level_candidates(
        snapshot_data,
        [
            ("support_price", "结构支撑"),
            ("value_area_low", "价值区下沿"),
            ("support_profile_price", "成交密集支撑"),
            ("window_vwap", "VWAP"),
            ("bid_wall_price", "买盘墙"),
        ],
    )
    resistance_candidates = _level_candidates(
        snapshot_data,
        [
            ("resistance_price", "结构压力"),
            ("value_area_high", "价值区上沿"),
            ("resistance_profile_price", "成交密集压力"),
            ("window_vwap", "VWAP"),
            ("ask_wall_price", "卖盘墙"),
        ],
    )

    if direction == "up":
        invalidation_level = _choose_level(support_candidates, price, above=False) or {"price": price, "label": "现价保护"}
        target_level = _choose_level(resistance_candidates, price, above=True)
        base_invalidation = float(invalidation_level["price"])
        invalidation_price = base_invalidation * (1 - buffer_bps / 10000)
        target_price = float(target_level["price"]) if target_level else 0.0
        invalidation_text = f"跌破{invalidation_level['label']}并无法快速收回，偏多判断失效。"
        target_text = (
            f"先看上方{target_level['label']}，站上后再看延续。"
            if target_level
            else "上方目标等待新结构确认。"
        )
        risk_bps = max(abs(_bps_change(price, invalidation_price)), 0.0)
        reward_bps = max(_bps_change(price, target_price), 0.0) if target_price > 0 else 0.0
    else:
        invalidation_level = _choose_level(resistance_candidates, price, above=True) or {"price": price, "label": "现价保护"}
        target_level = _choose_level(support_candidates, price, above=False)
        base_invalidation = float(invalidation_level["price"])
        invalidation_price = base_invalidation * (1 + buffer_bps / 10000)
        target_price = float(target_level["price"]) if target_level else 0.0
        invalidation_text = f"站上{invalidation_level['label']}并无法跌回，偏空判断失效。"
        target_text = (
            f"先看下方{target_level['label']}，跌破后再看延续。"
            if target_level
            else "下方目标等待新结构确认。"
        )
        risk_bps = max(abs(_bps_change(price, invalidation_price)), 0.0)
        reward_bps = max(-_bps_change(price, target_price), 0.0) if target_price > 0 else 0.0

    reward_risk = (reward_bps / risk_bps) if risk_bps > 0 and reward_bps > 0 else 0.0
    boundary_quality = "high" if invalidation_level["label"] != "现价保护" and target_price > 0 and local_volatility_bps > 0 else "medium" if invalidation_level["label"] != "现价保护" else "low"
    return {
        "directional": True,
        "invalidation_price": round(invalidation_price, 8),
        "target_price": round(target_price, 8) if target_price > 0 else None,
        "invalidation_bps": round(risk_bps, 3),
        "target_bps": round(reward_bps, 3) if reward_bps > 0 else None,
        "reward_risk": round(reward_risk, 2) if reward_risk > 0 else None,
        "buffer_bps": round(buffer_bps, 3),
        "buffer_components": buffer_components,
        "invalidation_basis": invalidation_level["label"],
        "target_basis": target_level["label"] if target_level else "",
        "boundary_quality": boundary_quality,
        "invalidation_text": invalidation_text,
        "target_text": target_text,
    }


class AlertStore:
    def __init__(
        self,
        path: str,
        snapshot_interval_seconds: int = 60,
        followup_resolver=None,
    ) -> None:
        self.path = Path(path)
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.followup_resolver = followup_resolver
        self._last_snapshot_at: dict[str, float] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL,
                    direction TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    bias TEXT NOT NULL,
                    event_time REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL,
                    direction TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    bias TEXT NOT NULL,
                    price REAL NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_followups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    target_time REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    anchor_price REAL NOT NULL,
                    close_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_bps REAL,
                    max_up_bps REAL,
                    max_down_bps REAL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    resolved_at REAL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(alert_id, horizon_minutes)
                )
                """
            )
            self._ensure_column(conn, "alerts", "event_time", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "alerts", "price", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(
                conn,
                "signal_snapshots",
                "updated_at",
                "REAL NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alerts_symbol_event_time
                ON alerts(symbol, event_time)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_updated_at
                ON signal_snapshots(symbol, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_followups_symbol_status_target
                ON alert_followups(symbol, status, target_time)
                """
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def record_event(self, event: AnomalyEvent, snapshot_data: dict | None = None) -> int:
        payload = asdict(event)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
        payload["ai_summary"] = list(payload.get("ai_summary", ()))
        payload["trigger_combo"] = trigger_combo(payload)
        payload["decision"] = _decision_metrics(event, snapshot_data)
        event_time = float(event.event_time or time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alerts(
                    symbol, score, direction, risk_level, bias, event_time, price, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.symbol,
                    event.score,
                    event.direction,
                    event.risk_level,
                    event.bias,
                    event_time,
                    event.price,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            alert_id = int(cursor.lastrowid)
            self._insert_followups(conn, alert_id, event.symbol, event_time, float(event.price))
        return alert_id

    def _insert_followups(
        self,
        conn: sqlite3.Connection,
        alert_id: int,
        symbol: str,
        event_time: float,
        anchor_price: float,
    ) -> None:
        for horizon_minutes in FOLLOWUP_HORIZONS_MINUTES:
            target_time = event_time + horizon_minutes * 60
            payload = {
                "label": _followup_label(horizon_minutes),
                "target_time": target_time,
            }
            conn.execute(
                """
                INSERT OR IGNORE INTO alert_followups(
                    alert_id, symbol, horizon_minutes, target_time, status, anchor_price, payload
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    alert_id,
                    symbol.upper(),
                    horizon_minutes,
                    target_time,
                    anchor_price,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, payload
                FROM alerts
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            followups_by_alert = self._load_followups(
                conn,
                [int(row[0]) for row in rows],
            )
            stats_by_signal = self._load_signal_stats(conn)
            stats_by_combo = self._load_combo_stats(conn)

        events = []
        for alert_id, created_at, payload in rows:
            data = json.loads(payload)
            data["created_at"] = created_at
            data["followups"] = [
                self._annotate_followup(data, item)
                for item in followups_by_alert.get(int(alert_id), [])
            ]
            stats_key = (
                str(data.get("symbol") or "").upper(),
                str(data.get("direction") or ""),
            )
            if stats_key in stats_by_signal:
                data["signal_stats"] = stats_by_signal[stats_key]
            combo = data.get("trigger_combo")
            if not isinstance(combo, dict):
                combo = trigger_combo(data)
                data["trigger_combo"] = combo
            combo_key = (
                str(data.get("direction") or ""),
                str(combo.get("key") or ""),
            )
            if combo_key in stats_by_combo:
                data["combo_stats"] = stats_by_combo[combo_key]
            events.append(data)
        return events

    def signal_context(self, data: dict | None) -> dict:
        payload = dict(data or {})
        combo = trigger_combo(payload)
        with self._connect() as conn:
            stats_by_signal = self._load_signal_stats(conn)
            stats_by_combo = self._load_combo_stats(conn)
        context = {"trigger_combo": combo}
        stats_key = (
            str(payload.get("symbol") or "").upper(),
            str(payload.get("direction") or ""),
        )
        combo_key = (
            str(payload.get("direction") or ""),
            str(combo.get("key") or ""),
        )
        if stats_key in stats_by_signal:
            context["signal_stats"] = stats_by_signal[stats_key]
        if combo_key in stats_by_combo:
            context["combo_stats"] = stats_by_combo[combo_key]
        return context

    @staticmethod
    def _annotate_followup(event: dict, item: dict) -> dict:
        direction = str(event.get("direction") or "")
        annotation = _directional_followup(direction, item)
        if annotation:
            return {**item, **annotation}
        return item

    def _load_signal_stats(self, conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
        rows = conn.execute(
            """
            SELECT
                a.symbol,
                a.direction,
                f.close_bps,
                f.max_up_bps,
                f.max_down_bps
            FROM alert_followups f
            JOIN alerts a ON a.id = f.alert_id
            WHERE f.status = 'resolved'
              AND f.horizon_minutes = ?
              AND a.direction IN ('up', 'down')
            ORDER BY f.resolved_at DESC
            LIMIT 800
            """,
            (STATS_HORIZON_MINUTES,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict]] = {}
        for symbol, direction, close_bps, max_up_bps, max_down_bps in rows:
            item = {
                "status": "resolved",
                "close_bps": close_bps,
                "max_up_bps": max_up_bps,
                "max_down_bps": max_down_bps,
            }
            metrics = _directional_followup(str(direction), item)
            if not metrics:
                continue
            grouped.setdefault((str(symbol).upper(), str(direction)), []).append(metrics)

        return self._stats_from_groups(grouped)

    def _load_combo_stats(self, conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
        rows = conn.execute(
            """
            SELECT
                a.direction,
                a.payload,
                f.close_bps,
                f.max_up_bps,
                f.max_down_bps
            FROM alert_followups f
            JOIN alerts a ON a.id = f.alert_id
            WHERE f.status = 'resolved'
              AND f.horizon_minutes = ?
              AND a.direction IN ('up', 'down')
            ORDER BY f.resolved_at DESC
            LIMIT 1200
            """,
            (STATS_HORIZON_MINUTES,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict]] = {}
        combo_labels: dict[tuple[str, str], str] = {}
        for direction, payload_text, close_bps, max_up_bps, max_down_bps in rows:
            try:
                payload = json.loads(payload_text or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            combo = payload.get("trigger_combo")
            if not isinstance(combo, dict):
                combo = trigger_combo(payload)
            combo_key = str(combo.get("key") or "")
            if not combo_key:
                continue
            item = {
                "status": "resolved",
                "close_bps": close_bps,
                "max_up_bps": max_up_bps,
                "max_down_bps": max_down_bps,
            }
            metrics = _directional_followup(str(direction), item)
            if not metrics:
                continue
            key = (str(direction), combo_key)
            grouped.setdefault(key, []).append(metrics)
            combo_labels.setdefault(key, str(combo.get("label") or combo_key))

        output = self._stats_from_groups(grouped)
        for key, value in output.items():
            value["combo_key"] = key[1]
            value["combo_label"] = combo_labels.get(key, key[1])
        return output

    @staticmethod
    def _stats_from_groups(grouped: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], dict]:
        output: dict[tuple[str, str], dict] = {}
        for key, items in grouped.items():
            sample_count = len(items)
            if sample_count <= 0:
                continue
            positive = [item for item in items if float(item["directional_close_bps"]) > 0]
            validated = [item for item in items if item.get("verdict") == "validated"]
            avg_close = sum(float(item["directional_close_bps"]) for item in items) / sample_count
            avg_favorable = sum(float(item["max_favorable_bps"]) for item in items) / sample_count
            avg_adverse = sum(float(item["max_adverse_bps"]) for item in items) / sample_count
            reliability = "high" if sample_count >= 30 else "medium" if sample_count >= 10 else "low"
            output[key] = {
                "horizon_minutes": STATS_HORIZON_MINUTES,
                "label": _followup_label(STATS_HORIZON_MINUTES),
                "sample_count": sample_count,
                "reliability": reliability,
                "win_rate": round(len(positive) / sample_count * 100, 1),
                "validated_rate": round(len(validated) / sample_count * 100, 1),
                "avg_close_bps": round(avg_close, 3),
                "avg_favorable_bps": round(avg_favorable, 3),
                "avg_adverse_bps": round(avg_adverse, 3),
            }
        return output

    def _load_followups(self, conn: sqlite3.Connection, alert_ids: list[int]) -> dict[int, list[dict]]:
        if not alert_ids:
            return {}
        placeholders = ",".join("?" for _ in alert_ids)
        rows = conn.execute(
            f"""
            SELECT
                alert_id,
                horizon_minutes,
                target_time,
                status,
                anchor_price,
                close_price,
                high_price,
                low_price,
                close_bps,
                max_up_bps,
                max_down_bps,
                sample_count,
                resolved_at,
                payload
            FROM alert_followups
            WHERE alert_id IN ({placeholders})
            ORDER BY alert_id DESC, horizon_minutes ASC
            """,
            alert_ids,
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            alert_id = int(row[0])
            grouped.setdefault(alert_id, []).append(
                {
                    "horizon_minutes": int(row[1]),
                    "label": _followup_label(int(row[1])),
                    "target_time": float(row[2]),
                    "status": str(row[3]),
                    "anchor_price": float(row[4] or 0),
                    "close_price": float(row[5] or 0) if row[5] is not None else None,
                    "high_price": float(row[6] or 0) if row[6] is not None else None,
                    "low_price": float(row[7] or 0) if row[7] is not None else None,
                    "close_bps": float(row[8] or 0) if row[8] is not None else None,
                    "max_up_bps": float(row[9] or 0) if row[9] is not None else None,
                    "max_down_bps": float(row[10] or 0) if row[10] is not None else None,
                    "sample_count": int(row[11] or 0),
                    "resolved_at": float(row[12] or 0) if row[12] is not None else None,
                    **self._decode_followup_payload(row[13]),
                }
            )
        return grouped

    @staticmethod
    def _decode_followup_payload(payload: str) -> dict:
        try:
            data = json.loads(payload or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def record_snapshot(self, snapshot: SymbolSnapshot) -> bool:
        last_recorded = self._last_snapshot_at.get(snapshot.symbol, 0.0)
        if snapshot.updated_at - last_recorded < self.snapshot_interval_seconds:
            return False

        payload = asdict(snapshot)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
        recorded_at = datetime.fromtimestamp(snapshot.updated_at).strftime("%Y-%m-%d %H:%M:%S")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signal_snapshots(
                    recorded_at, updated_at, symbol, score, direction, risk_level, bias, price, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    snapshot.updated_at,
                    snapshot.symbol,
                    snapshot.score,
                    snapshot.direction,
                    snapshot.risk_level,
                    snapshot.bias,
                    snapshot.price,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            resolved = self._resolve_due_followups(conn, snapshot.symbol, snapshot.updated_at)

        self._last_snapshot_at[snapshot.symbol] = snapshot.updated_at
        return resolved

    def _resolve_due_followups(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        now_ts: float,
    ) -> bool:
        due_rows = conn.execute(
            """
            SELECT
                f.id,
                f.alert_id,
                f.horizon_minutes,
                f.target_time,
                f.anchor_price,
                a.event_time
            FROM alert_followups f
            JOIN alerts a ON a.id = f.alert_id
            WHERE f.symbol = ? AND f.status = 'pending' AND f.target_time <= ?
            ORDER BY f.target_time ASC
            """,
            (symbol.upper(), now_ts),
        ).fetchall()
        resolved_any = False
        for row in due_rows:
            followup_id = int(row[0])
            horizon_minutes = int(row[2])
            target_time = float(row[3] or 0)
            anchor_price = float(row[4] or 0)
            event_time = float(row[5] or 0)
            if anchor_price <= 0 or event_time <= 0:
                continue

            exchange_result = self._resolve_followup_from_exchange(
                symbol=symbol.upper(),
                horizon_minutes=horizon_minutes,
                event_time=event_time,
                target_time=target_time,
                anchor_price=anchor_price,
            )
            if exchange_result:
                self._update_followup(conn, followup_id, horizon_minutes, exchange_result)
                resolved_any = True
                continue

            end_snapshot = conn.execute(
                """
                SELECT updated_at, price
                FROM signal_snapshots
                WHERE symbol = ? AND updated_at >= ?
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (symbol.upper(), target_time),
            ).fetchone()
            if not end_snapshot:
                continue

            close_time = float(end_snapshot[0] or 0)
            close_price = float(end_snapshot[1] or 0)
            if close_time <= 0 or close_price <= 0:
                continue

            samples = conn.execute(
                """
                SELECT price
                FROM signal_snapshots
                WHERE symbol = ? AND updated_at > ? AND updated_at <= ?
                ORDER BY updated_at ASC
                """,
                (symbol.upper(), event_time, close_time),
            ).fetchall()

            observed_prices = [anchor_price]
            for sample in samples:
                price = float(sample[0] or 0)
                if price > 0:
                    observed_prices.append(price)

            high_price = max(observed_prices)
            low_price = min(observed_prices)
            close_bps = _bps_change(anchor_price, close_price)
            max_up_bps = _bps_change(anchor_price, high_price)
            max_down_bps = _bps_change(anchor_price, low_price)
            sample_count = max(0, len(observed_prices) - 1)

            payload = {
                "label": _followup_label(horizon_minutes),
                "horizon_minutes": horizon_minutes,
                "target_time": target_time,
                "close_time": close_time,
                "anchor_price": anchor_price,
                "close_price": close_price,
                "high_price": high_price,
                "low_price": low_price,
                "close_bps": round(close_bps, 3),
                "max_up_bps": round(max_up_bps, 3),
                "max_down_bps": round(max_down_bps, 3),
                "sample_count": sample_count,
                "source": "signal_snapshots",
            }
            self._update_followup(conn, followup_id, horizon_minutes, payload)
            resolved_any = True
        return resolved_any

    def _resolve_followup_from_exchange(
        self,
        *,
        symbol: str,
        horizon_minutes: int,
        event_time: float,
        target_time: float,
        anchor_price: float,
    ) -> dict | None:
        if not self.followup_resolver:
            return None
        try:
            result = self.followup_resolver(
                {
                    "symbol": symbol,
                    "horizon_minutes": horizon_minutes,
                    "event_time": event_time,
                    "target_time": target_time,
                    "anchor_price": anchor_price,
                }
            )
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    def _update_followup(
        self,
        conn: sqlite3.Connection,
        followup_id: int,
        horizon_minutes: int,
        payload: dict,
    ) -> None:
        close_price = float(payload.get("close_price") or 0)
        high_price = float(payload.get("high_price") or 0)
        low_price = float(payload.get("low_price") or 0)
        close_bps = round(float(payload.get("close_bps") or 0), 3)
        max_up_bps = round(float(payload.get("max_up_bps") or 0), 3)
        max_down_bps = round(float(payload.get("max_down_bps") or 0), 3)
        sample_count = int(payload.get("sample_count") or 0)
        resolved_at = float(payload.get("close_time") or payload.get("target_time") or time())
        normalized_payload = dict(payload)
        normalized_payload.setdefault("label", _followup_label(horizon_minutes))
        normalized_payload.setdefault("horizon_minutes", horizon_minutes)
        conn.execute(
            """
            UPDATE alert_followups
            SET
                status = 'resolved',
                close_price = ?,
                high_price = ?,
                low_price = ?,
                close_bps = ?,
                max_up_bps = ?,
                max_down_bps = ?,
                sample_count = ?,
                resolved_at = ?,
                payload = ?
            WHERE id = ?
            """,
            (
                close_price,
                high_price,
                low_price,
                close_bps,
                max_up_bps,
                max_down_bps,
                sample_count,
                resolved_at,
                json.dumps(normalized_payload, ensure_ascii=False),
                followup_id,
            ),
        )
