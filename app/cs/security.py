# -*- coding: utf-8 -*-
"""坐席密码哈希、令牌生成与校验（仅用标准库）。"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_PBKDF2_ROUNDS = 200_000
SESSION_TTL_DAYS = 7


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS
    ).hex()
    return digest, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    if not password_hash or not salt:
        return False
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def new_token() -> str:
    """登录会话令牌 / 访客链接令牌（≥128 位熵）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
