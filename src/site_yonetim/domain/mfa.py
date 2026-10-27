"""İki adımlı doğrulama (TOTP, RFC 6238), yedek kodlar, zorunluluk ve step-up — saf
(Yazılım Dokümanı §4.7, PLT-01, PLT-09; Ürün Dokümanı §6.5).

- TOTP: SHA-1, 6 hane, 30 sn adım; saat kayması için ±1 adım kabul. Aynı adım iki kez
  kullanılamaz (tekrar oynatma) — çağıran son kullanılan adımı saklar.
- Yedek kodlar: 10 adet, `xxxx-xxxx` (32 harflik alfabe, 40 bit); yalnız SHA-256 özeti saklanır,
  her biri bir kez kullanılır.
- Zorunluluk: yönetici rolleri (yönetici, YK üyesi, denetçi, muhasebe, şirket rolleri, platform
  yöneticisi). Saha rolleri (güvenlik, teknik) ve sakin telefon + SMS koduyla girer.
- Step-up: iade, borç silme, rol yükseltme gibi işlemler son 5 dakikada yeniden doğrulama ister;
  telefon değişikliğinden sonraki 24 saatte finansal işlemlerde de.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # ±1 adım
SECRET_BYTES = 20  # 160 bit (RFC 4226 önerisi)
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # karışan harfler yok (i, l, o, 0, 1)

STEP_UP_WINDOW = timedelta(minutes=5)
PHONE_CHANGE_STEP_UP = timedelta(hours=24)

# Yönetici rolleri (Ürün Dokümanı §6.5: yönetici, muhasebe, YK, İK, şirket rolleri): rol düzeyi
# (K20, P18 — 1 en üst) bu değer ve üstündeyse 2FA zorunlu. Şablonlarda yönetici 1, YK 2,
# denetçi/muhasebe/İK 3; saha rolleri 4, sakin 9. Siteye özel rol kendi düzeyiyle değerlendirilir.
MFA_MAX_LEVEL = 3


def new_secret() -> str:
    """Base32 (dolgu yok) — doğrulayıcı uygulamaların beklediği biçim."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def hotp(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**TOTP_DIGITS:0{TOTP_DIGITS}d}"


def time_step(at: datetime) -> int:
    return int(at.timestamp()) // TOTP_PERIOD


def totp(secret: str, at: datetime) -> str:
    return hotp(secret, time_step(at))


def verify_totp(secret: str, code: str, at: datetime, *, last_step: int | None) -> int | None:
    """Kod geçerliyse eşleşen adımı döner (çağıran saklar), değilse None.

    Her aday adım sabit süreyle karşılaştırılır; `last_step` ve öncesi kabul edilmez."""
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) != TOTP_DIGITS:
        return None
    current = time_step(at)
    matched: int | None = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if hmac.compare_digest(hotp(secret, step), cleaned) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def otpauth_uri(secret: str, *, account: str, issuer: str) -> str:
    """Doğrulayıcı uygulamanın okuduğu QR içeriği."""
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": TOTP_DIGITS,
            "period": TOTP_PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.lower() if ch.isalnum())
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == 8 else cleaned


def recovery_digest(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode()).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    cleaned = "".join(ch for ch in code if ch.isalnum())
    return len(cleaned) == 8 and not cleaned.isdigit()


# --- Zorunluluk ----------------------------------------------------------------------


def requires_mfa(
    *, is_platform_admin: bool, role_levels: Iterable[int], has_organization_role: bool
) -> bool:
    """Bu kullanıcı yönetici rolünde mi (iki adımlı doğrulama zorunlu)?"""
    if is_platform_admin or has_organization_role:
        return True
    return any(level <= MFA_MAX_LEVEL for level in role_levels)


@dataclass(frozen=True, slots=True)
class MfaStatus:
    required: bool
    enabled: bool
    grace_until: datetime | None

    def setup_blocking(self, now: datetime) -> bool:
        """Kurulmadan devam edilemez mi? Geçiş süresi başlamadıysa (None) ya da bitmediyse hayır."""
        if not self.required or self.enabled or self.grace_until is None:
            return False
        return now >= self.grace_until

    def setup_pending(self) -> bool:
        return self.required and not self.enabled


def grace_deadline(now: datetime, grace_days: int) -> datetime:
    return now + timedelta(days=grace_days)


# --- Step-up -------------------------------------------------------------------------


def step_up_needed(
    *,
    verified_at: datetime | None,
    now: datetime,
    sensitive: bool = True,
    financial: bool = False,
    phone_changed_at: datetime | None = None,
) -> bool:
    """Yeniden doğrulama gerekiyor mu?

    - `sensitive` (iade, borç silme, rol yükseltme, dönem kilidi açma…): son 5 dakikada
      doğrulama yoksa evet.
    - `financial` (ödeme gibi olağan finansal işlem): yalnız telefon son 24 saatte değiştiyse
      aynı 5 dakika kuralı uygulanır (PLT-10 S2)."""
    stale = verified_at is None or now - verified_at > STEP_UP_WINDOW
    if sensitive:
        return stale
    if financial and phone_changed_at is not None and now - phone_changed_at < PHONE_CHANGE_STEP_UP:
        return stale
    return False
