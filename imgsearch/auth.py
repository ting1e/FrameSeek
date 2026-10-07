from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

from fastapi import HTTPException, Request


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
    return f"pbkdf2_sha256:600000:{salt.hex()}:{value.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split(":")
        if algorithm != "pbkdf2_sha256" or int(iterations) != 600000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, settings, db):
        if len(settings.session_secret) < 32 or not settings.password_hash:
            raise RuntimeError("Login not configured; run imgsearch init")
        self.settings = settings
        self.db = db
        self.key = settings.session_secret.encode()
        self.failures: list[float] = []
        self.lock = threading.Lock()

    def sign(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        body = base64.urlsafe_b64encode(json.dumps({
            "user": self.settings.username, "expires": time.time() + 12 * 3600,
            "nonce": secrets.token_hex(16),
        }).encode()).decode()
        return body + "." + self.sign(body)

    def csrf(self, token: str) -> str:
        return self.sign("csrf:" + token)

    def require(self, request: Request, mutation: bool = False) -> str:
        token = request.cookies.get("imgsearch_session", "")
        try:
            body, signature = token.rsplit(".", 1)
            if not hmac.compare_digest(signature, self.sign(body)):
                raise ValueError()
            decoded = json.loads(base64.urlsafe_b64decode(body))
            if decoded["expires"] < time.time() or decoded["user"] != self.settings.username:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "请先登录")
        if self.db.one('SELECT 1 FROM revoked_sessions WHERE token_hash=? AND expires>?',
                       (hashlib.sha256(token.encode()).hexdigest(), time.time())):
            raise HTTPException(401, "请先登录")
        if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), self.csrf(token).encode()):
            raise HTTPException(403, "CSRF 校验失败")
        return token

    def revoke(self, token: str):
        body = token.rsplit('.', 1)[0]
        expires = json.loads(base64.urlsafe_b64decode(body))['expires']
        with self.db.connect() as connection:
            connection.execute('DELETE FROM revoked_sessions WHERE expires<=?', (time.time(),))
            connection.execute('INSERT OR REPLACE INTO revoked_sessions VALUES(?,?)',
                               (hashlib.sha256(token.encode()).hexdigest(), expires))

    def login(self, request: Request, username: str, password: str) -> str:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "跨站登录请求被拒绝")
        now = time.time()
        # One login account: changing proxy headers must not reset its failure budget.
        # Keep verification under the lock so concurrent requests cannot exceed it.
        with self.lock:
            self.failures = [stamp for stamp in self.failures if stamp > now - 600]
            if len(self.failures) >= 5:
                raise HTTPException(429, "登录尝试过多，请稍后重试")
            if not (hmac.compare_digest(username.encode(), self.settings.username.encode()) and verify_password(password, self.settings.password_hash)):
                self.failures.append(now)
                raise HTTPException(401, "用户名或密码错误")
            self.failures.clear()
        return self.issue()
