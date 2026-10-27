"""Tek kullanımlık kod (SMS/e-posta) kuralları — saf (Yazılım Dokümanı §4.7, PLT-01).

- 6 hane, 3 dakika geçerli, kod başına en fazla 5 deneme.
- Aynı hedefe (telefon/e-posta) 60 saniyede bir yeni kod; günde (son 24 saat) en fazla 10 kod.
- 5 hatalı denemede hedef 15 dakika kilitlenir; kilitliyken kod istenemez ve doğrulanamaz.

Kodun kendisi saklanmaz: yalnız HMAC özeti (hedef ve amaca bağlı — başka bir hedefin
özetiyle eşleşmez). Karşılaştırma sabit sürelidir.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=3)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
DAILY_LIMIT = 10
DAILY_WINDOW = timedelta(hours=24)
LOCK_DURATION = timedelta(minutes=15)


class OtpPurpose(StrEnum):
    LOGIN = "login"  # telefonla giriş (sakin, saha)
    PASSWORD_RESET = "password_reset"  # noqa: S105  # nosec B105 — amaç adı, sır değil
    PHONE_CHANGE = "phone_change"
    STEP_UP = "step_up"  # hassas işlem öncesi yeniden doğrulama


class OtpChannel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


def new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def code_digest(key: bytes, purpose: OtpPurpose, target: str, code: str) -> str:
    message = f"{purpose.value}\x1f{target}\x1f{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def code_matches(key: bytes, purpose: OtpPurpose, target: str, code: str, digest: str) -> bool:
    """Sabit süreli karşılaştırma; biçimsiz kod da aynı yoldan geçer."""
    candidate = code_digest(key, purpose, target, code.strip())
    return hmac.compare_digest(candidate, digest)


class RequestBlock(StrEnum):
    LOCKED = "locked"
    TOO_SOON = "too_soon"
    DAILY_LIMIT = "daily_limit"


@dataclass(frozen=True, slots=True)
class RequestDecision:
    block: RequestBlock | None
    retry_after_seconds: int = 0


def request_decision(
    *,
    now: datetime,
    locked_until: datetime | None,
    last_sent_at: datetime | None,
    sent_last_24h: int,
    oldest_in_window: datetime | None,
) -> RequestDecision:
    """Bu hedefe şimdi yeni kod gönderilebilir mi? (adım 3: oran sınırı)."""
    if locked_until is not None and now < locked_until:
        return RequestDecision(RequestBlock.LOCKED, _seconds(locked_until - now))
    if last_sent_at is not None and now - last_sent_at < RESEND_AFTER:
        return RequestDecision(RequestBlock.TOO_SOON, _seconds(last_sent_at + RESEND_AFTER - now))
    if sent_last_24h >= DAILY_LIMIT:
        wait = (oldest_in_window + DAILY_WINDOW - now) if oldest_in_window else DAILY_WINDOW
        return RequestDecision(RequestBlock.DAILY_LIMIT, _seconds(wait))
    return RequestDecision(None)


def _seconds(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds() + 0.999))


class VerifyResult(StrEnum):
    OK = "ok"
    WRONG = "wrong"  # sayaç düştü, kalan deneme var
    LOCKED = "locked"  # 5. hatalı deneme ya da hedef kilitli
    EXPIRED = "expired"  # süresi doldu ya da kullanıldı / kod yok


@dataclass(frozen=True, slots=True)
class Verification:
    result: VerifyResult
    attempts: int
    remaining: int


def after_wrong_attempt(attempts: int) -> Verification:
    """Hatalı deneme sonrası durum: 5. hatada kilit."""
    count = attempts + 1
    if count >= MAX_ATTEMPTS:
        return Verification(VerifyResult.LOCKED, count, 0)
    return Verification(VerifyResult.WRONG, count, MAX_ATTEMPTS - count)
