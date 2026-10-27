"""Site iletişim ayarları: sessiz saatler ve SMS hesabı (Yazılım Dokümanı ILT-05, §16.2, PLT-05).

- Sessiz saatler: varsayılan 22:00–08:00; başlangıç ve bitiş aynı olamaz. Acil kategori aşar.
- SMS hesabı: varsayılan **platform hesabı** (paket kotası; aşım siteye faturalanır). Site isterse
  kendi sağlayıcı hesabını ve gönderici başlığını bağlar: sağlayıcı anahtarı şifreli saklanır,
  ekranda yalnız son 4 karakter. Sağlayıcı K5'te seçilene dek gönderim test sağlayıcısıyla
  yapılır (bağlanan hesap kaydedilir, gerçek gönderim yok).
- Aylık kota platform paketinden gelir (`SMS_DEFAULT_MONTHLY_QUOTA`); site ayarında saklanan
  değer yalnız platformca ezilir (A-08: kota miktarları pilot sonrası).
"""

import re
from datetime import time

from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.secretbox import seal
from site_yonetim.domain.delivery import DEFAULT_QUIET_FROM, DEFAULT_QUIET_TO, SmsAccountMode
from site_yonetim.models import MessagingSettings
from site_yonetim.services.delivery import site_settings

# Doküman §16.2'deki aday sağlayıcılar (K5: teklif aşaması). Hepsi şimdilik test sağlayıcısına
# yönlenir; seçim yapılınca gerçek adaptör `services/channels.py`'ye eklenir.
SMS_PROVIDERS = {"netgsm": "Netgsm", "iletimerkezi": "İleti Merkezi", "verimor": "Verimor"}
_SENDER = re.compile(r"^[A-Za-z0-9 .\-]{3,11}$")


class SettingsError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


async def get_or_create(session: AsyncSession) -> MessagingSettings:
    row = await site_settings(session)
    if row is None:
        row = MessagingSettings(
            quiet_from=DEFAULT_QUIET_FROM,
            quiet_to=DEFAULT_QUIET_TO,
            sms_mode=SmsAccountMode.PLATFORM.value,
        )
        session.add(row)
        await session.flush()
    return row


def parse_hm(value: str, field: str) -> time:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
    if match is None:
        raise SettingsError({field: "Saat SS:DD biçiminde olmalı (ör. 22:00)."})
    return time(int(match.group(1)), int(match.group(2)))


async def set_quiet_hours(
    session: AsyncSession, quiet_from: str, quiet_to: str
) -> MessagingSettings:
    errors: dict[str, str] = {}
    start = end = None
    try:
        start = parse_hm(quiet_from, "quiet_from")
    except SettingsError as exc:
        errors |= exc.fields
    try:
        end = parse_hm(quiet_to, "quiet_to")
    except SettingsError as exc:
        errors |= exc.fields
    if not errors and start == end:
        errors["quiet_to"] = "Başlangıç ve bitiş aynı olamaz."
    if errors or start is None or end is None:
        raise SettingsError(errors)
    row = await get_or_create(session)
    row.quiet_from = start
    row.quiet_to = end
    await session.flush()
    return row


async def set_sms_account(
    session: AsyncSession,
    *,
    mode: SmsAccountMode,
    sender: str | None,
    provider: str | None,
    api_key: str | None,
    key: bytes,
) -> MessagingSettings:
    row = await get_or_create(session)
    if mode is SmsAccountMode.PLATFORM:
        row.sms_mode = mode.value
        row.sms_sender = None
        row.sms_provider = None
        row.sms_api_key_sealed = None
        row.sms_api_key_last4 = None
        await session.flush()
        return row
    errors: dict[str, str] = {}
    cleaned_sender = (sender or "").strip()
    if not _SENDER.match(cleaned_sender):
        errors["sender"] = (
            "Gönderici başlığı 3–11 karakter olmalı (harf, rakam, boşluk, nokta, tire)."
        )
    if provider not in SMS_PROVIDERS:
        errors["provider"] = "Sağlayıcı seçin."
    secret = (api_key or "").strip()
    if not secret and row.sms_api_key_sealed is None:
        errors["api_key"] = "Sağlayıcının API anahtarını girin."
    elif secret and not 8 <= len(secret) <= 200:
        errors["api_key"] = "API anahtarı 8–200 karakter olmalı."
    if errors:
        raise SettingsError(errors)
    row.sms_mode = mode.value
    row.sms_sender = cleaned_sender
    row.sms_provider = provider
    if secret:
        row.sms_api_key_sealed = seal(key, secret)
        row.sms_api_key_last4 = secret[-4:]
    await session.flush()
    return row
