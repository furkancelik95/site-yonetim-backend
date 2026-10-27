"""Kısa sırların (iki adımlı doğrulama sırrı, sağlayıcı API anahtarı) veritabanında şifreli
saklanması — Yazılım Dokümanı §4.7 "Gizli bilgiler".

Yalnız standart kütüphane (yeni bağımlılık yok): HMAC-SHA256 tabanlı, ders kitabı "PRF ile
şifrele, sonra MAC" yapısı.

- Anahtar türetme: ana anahtardan HMAC ile iki ayrı anahtar (şifreleme, bütünlük).
- Şifreleme: her kayıtta 16 bayt rastgele nonce; anahtar akışı HMAC(k_enc, nonce ‖ sayaç).
- Bütünlük: HMAC(k_mac, sürüm ‖ nonce ‖ şifreli metin) — değiştirilmiş kayıt açılmaz.

Çıktı: `v1.<base64url>`. Düz metin hiçbir yerde loglanmaz.
"""

import base64
import hashlib
import hmac
import secrets

VERSION = b"v1"
_NONCE = 16
_TAG = 32
_MAX_PLAINTEXT = 4096


class SecretBoxError(ValueError):
    """Kayıt bozuk, değiştirilmiş ya da başka anahtarla şifrelenmiş."""


def _keys(master: bytes) -> tuple[bytes, bytes]:
    enc = hmac.new(master, b"site-yonetim/secretbox/enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"site-yonetim/secretbox/mac", hashlib.sha256).digest()
    return enc, mac


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(master: bytes, plaintext: str) -> str:
    data = plaintext.encode()
    if len(data) > _MAX_PLAINTEXT:
        raise ValueError("Şifrelenecek değer çok uzun.")
    enc, mac = _keys(master)
    nonce = secrets.token_bytes(_NONCE)
    cipher = bytes(a ^ b for a, b in zip(data, _stream(enc, nonce, len(data)), strict=True))
    tag = hmac.new(mac, VERSION + nonce + cipher, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(nonce + cipher + tag).decode().rstrip("=")


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
    nonce, cipher, tag = raw[:_NONCE], raw[_NONCE:-_TAG], raw[-_TAG:]
    enc, mac = _keys(master)
    expected = hmac.new(mac, VERSION + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise SecretBoxError
    data = bytes(a ^ b for a, b in zip(cipher, _stream(enc, nonce, len(cipher)), strict=True))
    return data.decode()
