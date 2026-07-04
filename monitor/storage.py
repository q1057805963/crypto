import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import time

from monitor.anomaly import AnomalyEvent, SymbolSnapshot


FOLLOWUP_HORIZONS_MINUTES = (5, 15, 60, 240, 1440)


def _bps_change(anchor_price: float, target_price: float) -> float:
    if anchor_price <= 0 or target_price <= 0:
        return 0.0
    return (target_price / anchor_price - 1) * 10000


def _followup_label(horizon_minutes: int) -> str:
    mapping = {
        5: "5m",
        15: "15m",
        60: "1h",
        240: "4h",
        1440: "1d",
    }
    return mapping.get(int(horizon_minutes), f"{int(horizon_minutes)}m")


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

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
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

    def record_event(self, event: AnomalyEvent) -> int:
        payload = asdict(event)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
        payload["ai_summary"] = list(payload.get("ai_summary", ()))
        event_time = float(event.event_time or time())
        with sqlite3.connect(self.path) as conn:
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
        with sqlite3.connect(self.path) as conn:
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

        events = []
        for alert_id, created_at, payload in rows:
            data = json.loads(payload)
            data["created_at"] = created_at
            data["followups"] = followups_by_alert.get(int(alert_id), [])
            events.append(data)
        return events

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

        with sqlite3.connect(self.path) as conn:
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
