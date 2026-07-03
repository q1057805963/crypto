import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from monitor.anomaly import AnomalyEvent, SymbolSnapshot


class AlertStore:
    def __init__(self, path: str, snapshot_interval_seconds: int = 60) -> None:
        self.path = Path(path)
        self.snapshot_interval_seconds = snapshot_interval_seconds
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
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
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

    def record_event(self, event: AnomalyEvent) -> None:
        payload = asdict(event)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
        payload["ai_summary"] = list(payload.get("ai_summary", ()))
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO alerts(symbol, score, direction, risk_level, bias, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.symbol,
                    event.score,
                    event.direction,
                    event.risk_level,
                    event.bias,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT created_at, payload
                FROM alerts
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        events = []
        for created_at, payload in rows:
            data = json.loads(payload)
            data["created_at"] = created_at
            events.append(data)
        return events

    def record_snapshot(self, snapshot: SymbolSnapshot) -> None:
        last_recorded = self._last_snapshot_at.get(snapshot.symbol, 0.0)
        if snapshot.updated_at - last_recorded < self.snapshot_interval_seconds:
            return

        payload = asdict(snapshot)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
        recorded_at = datetime.fromtimestamp(snapshot.updated_at).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO signal_snapshots(
                    recorded_at, symbol, score, direction, risk_level, bias, price, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    snapshot.symbol,
                    snapshot.score,
                    snapshot.direction,
                    snapshot.risk_level,
                    snapshot.bias,
                    snapshot.price,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

        self._last_snapshot_at[snapshot.symbol] = snapshot.updated_at
