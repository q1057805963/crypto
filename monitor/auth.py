import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from time import time
from typing import Any


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


class AuthError(Exception):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _json_b64(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(raw)


class AuthManager:
    def __init__(self, config: dict) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.allow_registration = bool(config.get("allow_registration", False))
        self.token_ttl_seconds = int(config.get("token_ttl_seconds", 7 * 24 * 3600))
        self.users_path = Path(str(config.get("users_path", "data/auth_users.json")))
        self.secret_path = Path(str(config.get("secret_path", "data/auth_secret.key")))
        self._lock = threading.Lock()
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret = self._load_secret(str(config.get("jwt_secret", "")))
        if not self.users_path.exists():
            self._write({"users": {}})

    def public_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "has_users": self.user_count() > 0,
            "allow_registration": self.can_register(),
        }

    def user_count(self) -> int:
        with self._lock:
            return len(self._read().get("users", {}))

    def can_register(self) -> bool:
        return self.allow_registration or self.user_count() == 0

    def register(self, username: str, password: str) -> dict:
        username = self._normalize_username(username)
        self._validate_password(password)
        with self._lock:
            data = self._read()
            users = data.setdefault("users", {})
            if username in users:
                raise AuthError("用户名已存在")
            if users and not self.allow_registration:
                raise AuthError("注册已关闭")

            user = {
                "user_id": f"u_{secrets.token_hex(16)}",
                "username": username,
                "password": self._hash_password(password),
                "role": "admin" if not users else "user",
                "created_at": int(time()),
            }
            users[username] = user
            self._write(data)
            return self._public_user(user)

    def login(self, username: str, password: str) -> dict:
        username = self._normalize_username(username)
        with self._lock:
            user = self._read().get("users", {}).get(username)
        if not user or not self._verify_password(password, user.get("password", {})):
            raise AuthError("用户名或密码错误")
        return self._public_user(user)

    def issue_token(self, user: dict) -> str:
        now = int(time())
        payload = {
            "sub": user["user_id"],
            "name": user["username"],
            "role": user.get("role", "user"),
            "iat": now,
            "exp": now + self.token_ttl_seconds,
        }
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = f"{_json_b64(header)}.{_json_b64(payload)}"
        signature = hmac.new(
            self.secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{signing_input}.{_b64url_encode(signature)}"

    def verify_token(self, token: str) -> dict:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".", 2)
            signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
            expected = hmac.new(
                self.secret.encode("utf-8"),
                signing_input,
                hashlib.sha256,
            ).digest()
            actual = _b64url_decode(signature_b64)
            if not hmac.compare_digest(expected, actual):
                raise AuthError("token签名无效")
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            if header.get("alg") != "HS256":
                raise AuthError("token算法无效")
            if int(payload.get("exp", 0)) < int(time()):
                raise AuthError("登录已过期")
            user_id = str(payload.get("sub", ""))
            with self._lock:
                for user in self._read().get("users", {}).values():
                    if user.get("user_id") == user_id:
                        return self._public_user(user)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError("token无效") from exc
        raise AuthError("用户不存在")

    @staticmethod
    def _normalize_username(username: str) -> str:
        value = str(username or "").strip().lower()
        if not USERNAME_PATTERN.match(value):
            raise AuthError("用户名需为3-32位英文、数字、下划线或短横线")
        return value

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(str(password or "")) < 8:
            raise AuthError("密码至少8位")

    @staticmethod
    def _public_user(user: dict) -> dict:
        return {
            "user_id": str(user.get("user_id", "")),
            "username": str(user.get("username", "")),
            "role": str(user.get("role", "user")),
        }

    @staticmethod
    def _hash_password(password: str) -> dict:
        salt = secrets.token_bytes(16)
        iterations = 210_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return {
            "algo": "pbkdf2_sha256",
            "iterations": iterations,
            "salt": _b64url_encode(salt),
            "hash": _b64url_encode(digest),
        }

    @staticmethod
    def _verify_password(password: str, config: dict[str, Any]) -> bool:
        try:
            iterations = int(config.get("iterations", 210_000))
            salt = _b64url_decode(str(config["salt"]))
            expected = _b64url_decode(str(config["hash"]))
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt,
                iterations,
            )
            return hmac.compare_digest(expected, actual)
        except Exception:
            return False

    def _load_secret(self, configured_secret: str) -> str:
        env_secret = os.environ.get("CFM_AUTH_SECRET", "").strip()
        if env_secret:
            return env_secret
        if configured_secret:
            return configured_secret
        try:
            secret = self.secret_path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        except FileNotFoundError:
            pass
        secret = secrets.token_urlsafe(48)
        self.secret_path.write_text(secret, encoding="utf-8")
        return secret

    def _read(self) -> dict:
        try:
            with self.users_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
                return data if isinstance(data, dict) else {"users": {}}
        except FileNotFoundError:
            return {"users": {}}
        except json.JSONDecodeError:
            return {"users": {}}

    def _write(self, data: dict) -> None:
        tmp_path = self.users_path.with_suffix(self.users_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        tmp_path.replace(self.users_path)
