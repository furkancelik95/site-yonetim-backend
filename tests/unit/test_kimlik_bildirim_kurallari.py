"""Saf kurallar: SMS kodu sınırları, TOTP, yedek kod, step-up, sessiz saat, SMS adedi, kota,
planlı rapor zamanı, şifreli saklama, telefon biçimi (Yazılım Dokümanı §4.7, PLT-05, RAP-05)."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from site_yonetim.core.config import Environment, Settings
from site_yonetim.core.secretbox import SecretBoxError, seal, unseal
from site_yonetim.domain.delivery import (
    QuotaUsage,
    Reachability,
    channels_for,
    in_quiet_hours,
    quiet_hours_end,
    send_time,
    sms_size,
    sms_text,
)
from site_yonetim.domain.mfa import (
    MfaStatus,
    hotp,
    looks_like_recovery_code,
    new_recovery_codes,
    otpauth_uri,
    recovery_digest,
    requires_mfa,
    step_up_needed,
    totp,
    verify_totp,
)
from site_yonetim.domain.notifications import (
    NotificationCategory,
    NotificationChannel,
    preference_matrix,
)
from site_yonetim.domain.otp import (
    OtpPurpose,
    RequestBlock,
    VerifyResult,
    after_wrong_attempt,
    code_digest,
    code_matches,
    new_code,
    request_decision,
)
from site_yonetim.domain.scheduled_reports import (
    ReportFrequency,
    ScheduleError,
    check_schedule,
    next_run,
    period_label,
)
from site_yonetim.domain.validation import normalize_phone

IST = ZoneInfo("Europe/Istanbul")
NOW = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


# --- SMS kodu ------------------------------------------------------------------------


def test_kod_6_hane_ve_ozet_hedefe_bagli() -> None:
    code = new_code()
    assert len(code) == 6
    assert code.isdigit()
    key = b"k" * 32
    digest = code_digest(key, OtpPurpose.LOGIN, "+905321234567", code)
    assert code not in digest
    assert code_matches(key, OtpPurpose.LOGIN, "+905321234567", code, digest)
    assert not code_matches(key, OtpPurpose.LOGIN, "+905329999999", code, digest)
    assert not code_matches(key, OtpPurpose.PASSWORD_RESET, "+905321234567", code, digest)


def test_istek_siniri() -> None:
    free = request_decision(
        now=NOW, locked_until=None, last_sent_at=None, sent_last_24h=0, oldest_in_window=None
    )
    assert free.block is None
    soon = request_decision(
        now=NOW, locked_until=None, last_sent_at=NOW - timedelta(seconds=20), sent_last_24h=1,
        oldest_in_window=NOW,
    )  # fmt: skip
    assert (soon.block, soon.retry_after_seconds) == (RequestBlock.TOO_SOON, 40)
    daily = request_decision(
        now=NOW, locked_until=None, last_sent_at=NOW - timedelta(hours=1), sent_last_24h=10,
        oldest_in_window=NOW - timedelta(hours=20),
    )  # fmt: skip
    assert (daily.block, daily.retry_after_seconds) == (RequestBlock.DAILY_LIMIT, 4 * 3600)
    locked = request_decision(
        now=NOW, locked_until=NOW + timedelta(minutes=15), last_sent_at=None, sent_last_24h=0,
        oldest_in_window=None,
    )  # fmt: skip
    assert locked.block is RequestBlock.LOCKED


def test_bes_hatada_kilit() -> None:
    assert after_wrong_attempt(0).remaining == 4
    assert after_wrong_attempt(3).result is VerifyResult.WRONG
    assert after_wrong_attempt(4).result is VerifyResult.LOCKED


# --- TOTP ve yedek kod -------------------------------------------------------------------

# RFC 6238 ek B test anahtarı: "12345678901234567890"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # gitleaks:allow — RFC test vektörü


def test_rfc_6238_vektorleri() -> None:
    assert hotp(RFC_SECRET, 1) == "287082"
    assert totp(RFC_SECRET, datetime.fromtimestamp(59, UTC)) == "287082"
    assert totp(RFC_SECRET, datetime.fromtimestamp(1111111109, UTC)) == "081804"


def test_totp_pencere_ve_tekrar() -> None:
    code = totp(RFC_SECRET, NOW)
    step = verify_totp(RFC_SECRET, code, NOW + timedelta(seconds=30), last_step=None)
    assert step is not None
    assert verify_totp(RFC_SECRET, code, NOW, last_step=step) is None  # tekrar oynatma
    assert verify_totp(RFC_SECRET, code, NOW + timedelta(minutes=5), last_step=None) is None
    assert verify_totp(RFC_SECRET, "12a", NOW, last_step=None) is None
    uri = otpauth_uri(RFC_SECRET, account="a@b.c", issuer="SiteYönetimi")
    assert uri.startswith("otpauth://totp/SiteY%C3%B6netimi%3Aa%40b.c?secret=")


def test_yedek_kodlar() -> None:
    codes = new_recovery_codes()
    assert len(set(codes)) == 10
    assert all(len(c) == 9 and c[4] == "-" for c in codes)
    assert recovery_digest(codes[0].upper().replace("-", "")) == recovery_digest(codes[0])
    assert looks_like_recovery_code(codes[0])
    assert not looks_like_recovery_code("123456")


def test_zorunluluk_ve_gecis_suresi() -> None:
    assert requires_mfa(is_platform_admin=False, role_levels=[3], has_organization_role=False)
    assert not requires_mfa(
        is_platform_admin=False, role_levels=[9, 4], has_organization_role=False
    )
    assert requires_mfa(is_platform_admin=True, role_levels=[], has_organization_role=False)
    pending = MfaStatus(required=True, enabled=False, grace_until=NOW)
    assert pending.setup_pending()
    assert pending.setup_blocking(NOW)
    assert not pending.setup_blocking(NOW - timedelta(seconds=1))
    assert not MfaStatus(True, False, None).setup_blocking(NOW)
    assert not MfaStatus(True, True, NOW).setup_blocking(NOW)


def test_step_up() -> None:
    assert step_up_needed(verified_at=None, now=NOW)
    assert not step_up_needed(verified_at=NOW - timedelta(minutes=4), now=NOW)
    assert step_up_needed(verified_at=NOW - timedelta(minutes=6), now=NOW)
    # olağan finansal işlem: yalnız telefon 24 saatte değiştiyse
    old = NOW - timedelta(hours=8)
    assert not step_up_needed(verified_at=old, now=NOW, sensitive=False, financial=True)
    assert step_up_needed(
        verified_at=old, now=NOW, sensitive=False, financial=True,
        phone_changed_at=NOW - timedelta(hours=6),
    )  # fmt: skip
    assert not step_up_needed(
        verified_at=old, now=NOW, sensitive=False, financial=True,
        phone_changed_at=NOW - timedelta(hours=25),
    )  # fmt: skip


# --- Gönderim -------------------------------------------------------------------------


def test_sessiz_saat() -> None:
    assert in_quiet_hours(time(23, 10), time(22), time(8))
    assert in_quiet_hours(time(7, 59), time(22), time(8))
    assert not in_quiet_hours(time(8), time(22), time(8))
    assert in_quiet_hours(time(13), time(12), time(14))
    night = datetime(2026, 6, 20, 20, 10, tzinfo=UTC)  # 23:10 İstanbul (S1)
    assert quiet_hours_end(night, IST, time(22), time(8)) == datetime(2026, 6, 21, 5, tzinfo=UTC)
    after_midnight = datetime(2026, 6, 20, 22, 30, tzinfo=UTC)  # 01:30 İstanbul
    assert quiet_hours_end(after_midnight, IST, time(22), time(8)) == datetime(
        2026, 6, 21, 5, tzinfo=UTC
    )
    assert send_time(NotificationCategory.URGENT, night, IST, time(22), time(8)) == night


def test_kanal_secimi() -> None:
    m = preference_matrix({})
    none = Reachability(False, None, None)
    assert channels_for(NotificationCategory.OPERATIONS, m, none) == []
    phone_only = Reachability(False, "+905321234567", None)
    assert channels_for(NotificationCategory.URGENT, m, phone_only) == [NotificationChannel.SMS]
    push_phone = Reachability(True, "+905321234567", "a@b.c")
    assert channels_for(NotificationCategory.URGENT, m, push_phone) == [NotificationChannel.PUSH]
    assert channels_for(NotificationCategory.FINANCIAL, m, push_phone) == [
        NotificationChannel.PUSH,
        NotificationChannel.EMAIL,
    ]


def test_sms_adedi_ve_site_adi() -> None:
    assert sms_size("a" * 160).segments == 1
    assert sms_size("a" * 161).segments == 2
    turkish = sms_size("Şifre ğüı")
    assert (turkish.encoding, turkish.segments) == ("Unicode", 1)
    assert sms_size("ş" * 71).segments == 2
    assert sms_text("Aksu", "Merhaba") == "Aksu: Merhaba"


def test_kota() -> None:
    usage = QuotaUsage(quota=100, used=85)
    assert (usage.remaining, usage.overage, usage.percent) == (15, 0, 85)
    assert usage.crossed(79) == [80]
    assert QuotaUsage(100, 101).crossed(85) == [100]
    assert QuotaUsage(100, 101).overage == 1
    assert QuotaUsage(100, 30).forecast(day=10, days_in_month=30) == 90


# --- Planlı rapor -----------------------------------------------------------------------


def test_planli_rapor_zamani() -> None:
    after = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)  # Cumartesi 12:00 İstanbul
    daily = next_run(ReportFrequency.DAILY, send_time=time(8, 30), weekday=None,
                     day_of_month=None, after=after, tz=IST)  # fmt: skip
    assert daily == datetime(2026, 6, 21, 5, 30, tzinfo=UTC)
    weekly = next_run(ReportFrequency.WEEKLY, send_time=time(9), weekday=0,
                      day_of_month=None, after=after, tz=IST)  # fmt: skip
    assert weekly.astimezone(IST).weekday() == 0
    monthly = next_run(ReportFrequency.MONTHLY, send_time=time(9), weekday=None,
                       day_of_month=1, after=after, tz=IST)  # fmt: skip
    assert monthly == datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
    assert period_label(ReportFrequency.MONTHLY, monthly, IST) == "06.2026"
    with pytest.raises(ScheduleError):
        check_schedule(ReportFrequency.MONTHLY, None, 31)


# --- Şifreli saklama, telefon, ayar -----------------------------------------------------


def test_secretbox() -> None:
    sealed = seal(b"anahtar", "JBSWY3DPEHPK3PXP")
    assert "JBSWY3DP" not in sealed
    assert unseal(b"anahtar", sealed) == "JBSWY3DPEHPK3PXP"
    assert seal(b"anahtar", "x") != seal(b"anahtar", "x")  # rastgele nonce
    with pytest.raises(SecretBoxError):
        unseal(b"baska", sealed)
    with pytest.raises(SecretBoxError):
        unseal(b"anahtar", sealed[:-2] + "AA")


def test_telefon_bicimi() -> None:
    assert normalize_phone("0532 123 45 67") == "+905321234567"
    assert normalize_phone("+90 532 123 4567") == "+905321234567"
    assert normalize_phone("+49 151 23456789") == "+4915123456789"
    with pytest.raises(ValueError, match="ülke koduyla"):
        normalize_phone("+12")


def test_uretimde_test_kutusu_acilamaz() -> None:
    base = {"jwt_secret": "x" * 40, "environment": "production", "_env_file": None}
    assert Settings(**base).test_inbox_active is False  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="TEST_INBOX_ENABLED"):
        Settings(**base, test_inbox_enabled=True)  # type: ignore[arg-type]
    dev = Settings(_env_file=None, environment=Environment.DEVELOPMENT)
    assert dev.test_inbox_active is True
