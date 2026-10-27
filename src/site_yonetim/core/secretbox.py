"""Kısa sırların (iki adımlı doğrulama sırrı, sağlayıcı API anahtarı) veritabanında şifreli
saklanması — Yazılım Dokümanı §4.7 "Gizli bilgiler".

`cryptography` kütüphanesinin AES-256-GCM'i (kimlik doğrulamalı şifreleme); kendi şifreleme
yapımız yok.

- Anahtar: ana anahtardan (`DATA_ENCRYPTION_KEY`, yoksa JWT sırrı) HKDF-SHA256 ile 32 bayt.
- Her kayıtta 12 bayt rastgele nonce; sürüm etiketi ek veri (AAD) olarak doğrulanır.
- Değiştirilmiş ya da başka anahtarla şifrelenmiş kayıt açılmaz (`SecretBoxError`).

Çıktı: `v2.<base64url(nonce ‖ şifreli metin ‖ etiket)>`. Düz metin hiçbir yerde loglanmaz.
"""

import base64
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = b"v2"
_NONCE = 12
_TAG = 16
_MAX_PLAINTEXT = 4096
_INFO = b"site-yonetim/secretbox/aes-256-gcm"


class SecretBoxError(ValueError):
    """Kayıt bozuk, değiştirilmiş ya da başka anahtarla şifrelenmiş."""


def _cipher(master: bytes) -> AESGCM:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(master)
    return AESGCM(key)


def seal(master: bytes, plaintext: str) -> str:
    data = plaintext.encode()
    if len(data) > _MAX_PLAINTEXT:
        raise ValueError("Şifrelenecek değer çok uzun.")
    nonce = secrets.token_bytes(_NONCE)
    sealed = _cipher(master).encrypt(nonce, data, VERSION)
    return VERSION.decode() + "." + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")


def unseal(master: bytes, sealed: str) -> str:
    prefix, _, body = sealed.partition(".")
    if prefix != VERSION.decode() or not body:
        raise SecretBoxError
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError as exc:
        raise SecretBoxError from exc
    if len(raw) < _NONCE + _TAG:
        raise SecretBoxError
    try:
        data = _cipher(master).decrypt(raw[:_NONCE], raw[_NONCE:], VERSION)
    except InvalidTag as exc:
        raise SecretBoxError from exc
    return data.decode()
