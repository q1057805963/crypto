import copy
import json
import re
import threading
from pathlib import Path
from time import time
from typing import Any


USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _safe_user_id(user_id: str | None) -> str:
    value = str(user_id or "").strip()
    if USER_ID_PATTERN.match(value):
        return value
    return "default_user"


def _without_secret_ai(ai_config: dict) -> dict:
    ai = copy.deepcopy(ai_config)
    ai["api_key"] = ""
    return ai


class UserConfigStore:
    def __init__(self, path: str, base_config: dict) -> None:
        self.path = Path(path)
        self.base_config = base_config
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"users": {}})

    def get(self, user_id: str | None) -> dict:
        safe_id = _safe_user_id(user_id)
        with self._lock:
            data = self._read()
            users = data.setdefault("users", {})
            if safe_id not in users:
                users[safe_id] = self._default_user_config(safe_id)
                self._write(data)
            return copy.deepcopy(users[safe_id])

    def has(self, user_id: str | None) -> bool:
        safe_id = _safe_user_id(user_id)
        with self._lock:
            return safe_id in self._read().get("users", {})

    def update_section(self, user_id: str | None, section: str, value: Any) -> dict:
        safe_id = _safe_user_id(user_id)
        with self._lock:
            data = self._read()
            users = data.setdefault("users", {})
            user = users.setdefault(safe_id, self._default_user_config(safe_id))
            user[section] = copy.deepcopy(value)
            user["updated_at"] = time()
            self._write(data)
            return copy.deepcopy(user)

    def update_symbols(self, user_id: str | None, symbols: list[str]) -> dict:
        return self.update_section(user_id, "symbols", symbols)

    def update_telegram(self, user_id: str | None, telegram: dict) -> dict:
        return self.update_section(user_id, "telegram", telegram)

    def update_ai(self, user_id: str | None, ai: dict) -> dict:
        return self.update_section(user_id, "ai", ai)

    def update_symbol_thresholds(self, user_id: str | None, thresholds: dict) -> dict:
        return self.update_section(user_id, "symbol_thresholds", thresholds)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._read().get("users", {}))

    def all_symbols(self) -> list[str]:
        symbols = []
        seen = set()
        for user in self.all().values():
            for symbol in user.get("symbols", []):
                upper = str(symbol).upper()
                if upper and upper not in seen:
                    symbols.append(upper)
                    seen.add(upper)
        if not symbols:
            symbols = [str(symbol).upper() for symbol in self.base_config.get("symbols", [])]
        return symbols

    def aggregate_symbol_thresholds(self) -> dict:
        aggregated: dict[str, dict[str, float]] = {}
        for user in self.all().values():
            for symbol, config in (user.get("symbol_thresholds") or {}).items():
                if not isinstance(config, dict):
                    continue
                score = config.get("anomaly_score")
                if score is None:
                    continue
                symbol = str(symbol).upper()
                current = aggregated.get(symbol, {}).get("anomaly_score")
                score = float(score)
                if current is None or score < current:
                    aggregated[symbol] = {"anomaly_score": score}
        return aggregated

    def aggregate_telegram_users(self, default_score: float) -> list[dict]:
        users = []
        for user_id, user in self.all().items():
            telegram = user.get("telegram") or {}
            if not telegram.get("enabled", False):
                continue
            for tg_user in telegram.get("users", []):
                item = copy.deepcopy(tg_user)
                item["owner_id"] = user_id
                item["symbols"] = list(user.get("symbols", []))
                item["symbol_thresholds"] = copy.deepcopy(user.get("symbol_thresholds", {}))
                item["default_score"] = float(default_score)
                users.append(item)
        return users

    def _default_user_config(self, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "created_at": time(),
            "updated_at": time(),
            "symbols": [str(symbol).upper() for symbol in self.base_config.get("symbols", [])],
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "chat_ids": [],
                "users": [],
            },
            "ai": _without_secret_ai(self.base_config.get("ai", {})),
            "symbol_thresholds": {},
            "dashboard": {
                "theme": (self.base_config.get("dashboard") or {}).get("theme", "dark"),
            },
        }

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
                return data if isinstance(data, dict) else {"users": {}}
        except FileNotFoundError:
            return {"users": {}}
        except json.JSONDecodeError:
            return {"users": {}}

    def _write(self, data: dict) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        tmp_path.replace(self.path)
