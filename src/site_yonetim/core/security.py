"""Parola hash'i, erişim jetonu (JWT) ve yenileme jetonu (docs/05 §8, docs/09 §2).

- Parola: argon2id. Düz metin ya da geri çözülebilir şifreleme yok.
- Erişim jetonu: HS256 JWT, kısa ömürlü (JWT_ACCESS_MINUTES, varsayılan 15 dk). Frontend
  bellekte tutar; `localStorage`'a yazılmaz.
- Yenileme jetonu: 256 bit rastgele, opak. Veritabanında yalnız SHA-256 hash'i durur.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from site_yonetim.core.config import Settings

_hasher = PasswordHasher()  # argon2id, kütüphanenin güncel önerilen parametreleri
# Bilinmeyen e-postada da aynı sürede yanıt vermek için doğrulanan sahte hash.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "site-yonetim"
JWT_AUDIENCE = "site-yonetim-api"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Sabit süreli doğrulama. Hash yoksa sahte hash doğrulanır (kullanıcı sayımı engeli)."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


class InvalidTokenError(Exception):
    """Jeton geçersiz, süresi dolmuş ya da bu uygulamaya ait değil."""


def create_access_token(
    settings: Settings, *, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime
) -> str:
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": "access",
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # "none" ve algoritma karıştırma saldırısına kapalı
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "sid", "typ", "iss", "aud"]},
        )
        if payload["typ"] != "access":
            raise InvalidTokenError
        return AccessClaims(uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise InvalidTokenError from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
