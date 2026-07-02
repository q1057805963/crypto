import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from monitor.anomaly import AnomalyEvent


class AlertStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
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

    def record_event(self, event: AnomalyEvent) -> None:
        payload = asdict(event)
        payload["reasons"] = list(payload["reasons"])
        payload["suggestions"] = list(payload["suggestions"])
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
