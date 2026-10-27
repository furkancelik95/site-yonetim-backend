"""İletişim ayarları, SMS hesabı ve kota, teslim kayıtları — Yazılım Dokümanı ILT-05, PLT-05,
§16.2 (servis isteği 71'in sessiz saat kısmı; doküman API taslağı `…/settings/sms`).

- `GET|PUT …/communication-settings` — sessiz saatler (servis isteği 71 biçimi).
- `GET|PUT …/settings/sms` — SMS hesabı (platform kotası / kendi hesabı), başlık, anahtarın son
  4 karakteri; `POST …/settings/sms/test` test SMS'i.
- `GET …/settings/sms/usage` — bu ayın kullanımı, kota, aşım, ay sonu tahmini.
- `GET …/notification-deliveries` — teslim kayıtları (kanal, durum, maliyet).
- `GET /platform/test-inbox` — test sağlayıcısının son mesajları (yalnız geliştirme/demo,
  platform yöneticisi; üretimde 404).

Sağlayıcı açık karar K5'te seçilecek: yanıtlarda `test_provider: true` — gönderim kayda geçer,
dışarı gitmez.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    ChannelsDep,
    CurrentUserDep,
    FactoryDep,
    NowDep,
    PlatformAdminDep,
    SettingsDep,
    SiteContext,
    SiteContextDep,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.delivery import (
    DELIVERY_STATUS_LABELS,
    DeliveryStatus,
    SmsAccountMode,
    sms_size,
)
from site_yonetim.domain.masking import mask_email, mask_phone
from site_yonetim.domain.notifications import CATEGORIES, NotificationCategory, NotificationChannel
from site_yonetim.domain.validation import normalize_phone
from site_yonetim.models import MessagingSettings, NotificationDelivery
from site_yonetim.services import delivery as delivery_svc
from site_yonetim.services import messaging as svc
from site_yonetim.services.channels import TEST_INBOX

router = APIRouter(prefix="/sites/{slug}", tags=["bildirim gönderimi"])
platform_router = APIRouter(prefix="/platform", tags=["platform"])

Manage = Annotated[SiteContext, Depends(require_permission(Permission.NOTIFICATIONS_MANAGE))]
Paging = Annotated[PageParams, Depends()]
FORM_ERROR = "Formda düzeltilmesi gereken alanlar var."


def _form(exc: svc.SettingsError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def _hm(value: dt.time) -> str:
    return value.strftime("%H:%M")


# --- Sessiz saatler ----------------------------------------------------------------------


class CommunicationSettingsOut(BaseModel):
    quiet_from: str = Field(description='"SS:DD"; aralık gece yarısını geçebilir')
    quiet_to: str


class CommunicationSettingsIn(BaseModel):
    quiet_from: str = Field(max_length=5)
    quiet_to: str = Field(max_length=5)


def _comm(row: MessagingSettings | None) -> CommunicationSettingsOut:
    quiet_from, quiet_to = delivery_svc.quiet_hours(row)
    return CommunicationSettingsOut(quiet_from=_hm(quiet_from), quiet_to=_hm(quiet_to))


@router.get("/communication-settings", summary="Sessiz saatler")
async def communication_settings(ctx: SiteContextDep) -> CommunicationSettingsOut:
    """Sitedeki herkes okur (duyuru ekranı bilgi olarak gösterir)."""
    return _comm(await delivery_svc.site_settings(ctx.session))


@router.put("/communication-settings", summary="Sessiz saatleri kaydet")
async def update_communication_settings(
    body: CommunicationSettingsIn, ctx: Manage
) -> Written[CommunicationSettingsOut]:
    try:
        row = await svc.set_quiet_hours(ctx.session, body.quiet_from, body.quiet_to)
    except svc.SettingsError as exc:
        raise _form(exc) from exc
    out = _comm(row)
    await ctx.session.commit()
    return Written(data=out, message="Sessiz saatler kaydedildi.")


# --- SMS hesabı ----------------------------------------------------------------------------


class SmsProviderOption(BaseModel):
    key: str
    name: str


class SmsAccountOut(BaseModel):
    mode: SmsAccountMode
    sender: str | None
    provider: str | None
    api_key_last4: str | None = Field(description="yalnız son 4 karakter (§4.7)")
    monthly_quota: int
    quota_source: str = Field(description="platform: paket kotası · site: sitenin ayarı")
    providers: list[SmsProviderOption]
    test_provider: bool = Field(
        description="true: sağlayıcı seçilene dek (K5) gönderim kayda geçer, dışarı gitmez"
    )
    platform_sender: str = Field(description="platform hesabında gönderici başlığı")


class SmsAccountIn(BaseModel):
    mode: SmsAccountMode
    sender: str | None = Field(default=None, max_length=20)
    provider: str | None = Field(default=None, max_length=40)
    api_key: str | None = Field(
        default=None, max_length=200, description="boş → kayıtlı anahtar korunur"
    )


def _account(row: MessagingSettings | None, default_quota: int) -> SmsAccountOut:
    return SmsAccountOut(
        mode=SmsAccountMode(row.sms_mode) if row else SmsAccountMode.PLATFORM,
        sender=row.sms_sender if row else None,
        provider=row.sms_provider if row else None,
        api_key_last4=row.sms_api_key_last4 if row else None,
        monthly_quota=delivery_svc.monthly_quota(row, default_quota),
        quota_source="site" if row and row.sms_monthly_quota is not None else "platform",
        providers=[SmsProviderOption(key=k, name=v) for k, v in svc.SMS_PROVIDERS.items()],
        test_provider=True,
        platform_sender="SITEYONETIM",
    )


@router.get("/settings/sms", summary="SMS hesabı")
async def sms_account(ctx: Manage, settings: SettingsDep) -> SmsAccountOut:
    return _account(
        await delivery_svc.site_settings(ctx.session), settings.sms_default_monthly_quota
    )


@router.put("/settings/sms", summary="SMS hesabını kaydet")
async def update_sms_account(
    body: SmsAccountIn, ctx: Manage, settings: SettingsDep
) -> Written[SmsAccountOut]:
    try:
        row = await svc.set_sms_account(
            ctx.session,
            mode=body.mode,
            sender=body.sender,
            provider=body.provider,
            api_key=body.api_key,
            key=settings.encryption_key,
        )
    except svc.SettingsError as exc:
        raise _form(exc) from exc
    out = _account(row, settings.sms_default_monthly_quota)
    await ctx.session.commit()
    message = (
        "Platform SMS hesabı kullanılacak (paket kotası)."
        if body.mode is SmsAccountMode.PLATFORM
        else "Kendi SMS hesabınız kaydedildi. Test SMS'i göndererek deneyin."
    )
    return Written(data=out, message=message)


class SmsTestIn(BaseModel):
    phone: str | None = Field(default=None, max_length=32, description="boş → kendi numaranız")


class SmsTestOut(BaseModel):
    delivery_id: uuid.UUID
    recipient: str
    status: DeliveryStatus
    segments: int
    test_provider: bool = True


@router.post("/settings/sms/test", summary="Test SMS'i gönder")
async def send_test_sms(
    body: SmsTestIn,
    ctx: Manage,
    current: CurrentUserDep,
    settings: SettingsDep,
    factory: FactoryDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[SmsTestOut]:
    raw = body.phone or current.user.phone
    if not raw:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {"phone": "Test SMS'i için telefon numarası girin."},
        )
    try:
        phone = normalize_phone(raw)
    except ValueError as exc:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, {"phone": str(exc)}
        ) from None
    d = await delivery_svc.queue_direct(
        ctx.session,
        channel=NotificationChannel.SMS,
        to=phone,
        body="Test mesajı: SMS hesabınız çalışıyor.",
        category=NotificationCategory.OPERATIONS,
        kind="sms.test",
        user_id=current.user.id,
        now=now,
    )
    delivery_id = d.id
    await ctx.session.commit()
    ctx.session.info.pop(delivery_svc.DISPATCH_KEY, None)  # hemen gönderiyoruz
    await delivery_svc.dispatch_due(
        factory,
        channels,
        now=now,
        default_quota=settings.sms_default_monthly_quota,
        site_ids=[ctx.site.id],
    )
    sent = await ctx.session.get(NotificationDelivery, delivery_id, populate_existing=True)
    if sent is None:  # pragma: no cover
        raise NotFoundError
    return Written(
        data=SmsTestOut(
            delivery_id=sent.id,
            recipient=mask_phone(phone),
            status=DeliveryStatus(sent.status),
            segments=sent.cost_units or sms_size(sent.body).segments,
        ),
        message="Test SMS'i gönderildi (test sağlayıcısı: kayda geçti, dışarı gitmedi — K5)."
        if sent.status == DeliveryStatus.SENT.value
        else "Test SMS'i gönderilemedi.",
    )


class CategoryUsage(BaseModel):
    category: NotificationCategory
    label: str
    units: int


class SmsUsageOut(BaseModel):
    month: str = Field(description="YYYY-MM")
    quota: int
    used: int = Field(description="bu ay gönderilen SMS adedi (uzun mesaj birden çok sayılır)")
    remaining: int
    overage: int = Field(description="kota aşımı (sonraki faturaya yansır)")
    percent: int
    forecast: int = Field(description="ay sonu tahmini (bugüne kadarki kullanımın uzatması)")
    messages: int
    queued: int = Field(description="kuyrukta bekleyen teslim (sessiz saat dahil)")
    by_category: list[CategoryUsage]
    warn_percents: list[int] = Field(description="uyarı eşikleri (%80/%100)")


@router.get("/settings/sms/usage", summary="SMS kullanımı ve kota")
async def sms_usage(ctx: Manage, settings: SettingsDep, now: NowDep) -> SmsUsageOut:
    u = await delivery_svc.month_usage(
        ctx.session, now=now, default_quota=settings.sms_default_monthly_quota
    )
    return SmsUsageOut(
        month=u.month,
        quota=u.usage.quota,
        used=u.usage.used,
        remaining=u.usage.remaining,
        overage=u.usage.overage,
        percent=u.usage.percent,
        forecast=u.forecast,
        messages=u.sms_count,
        queued=u.queued,
        by_category=[
            CategoryUsage(
                category=NotificationCategory(c),
                label=CATEGORIES[NotificationCategory(c)].label,
                units=n,
            )
            for c, n in sorted(u.by_category.items(), key=lambda x: -x[1])
        ],
        warn_percents=[80, 100],
    )


class DeliveryOut(BaseModel):
    id: uuid.UUID
    created_at: dt.datetime
    channel: NotificationChannel
    category: NotificationCategory
    kind: str
    recipient: str = Field(description="maskeli adres")
    subject: str | None
    body: str
    status: DeliveryStatus
    status_label: str
    is_critical: bool
    deferred: bool = Field(description="sessiz saat nedeniyle ertelendi")
    scheduled_for: dt.datetime
    sent_at: dt.datetime | None
    provider: str | None
    cost_units: int
    is_overage: bool
    is_fallback: bool = Field(description="kritik bildirimin SMS yedeği")
    error: str | None


def _mask(channel: str, value: str) -> str:
    if channel == NotificationChannel.SMS.value:
        return mask_phone(value)
    if channel == NotificationChannel.EMAIL.value:
        return mask_email(value)
    return value


@router.get("/notification-deliveries", summary="Teslim kayıtları (push/SMS/e-posta)")
async def deliveries(
    ctx: Manage,
    paging: Paging,
    channel: Annotated[NotificationChannel | None, Query()] = None,
    status: Annotated[DeliveryStatus | None, Query()] = None,
) -> Page[DeliveryOut]:
    query = delivery_svc.deliveries_query(channel=channel, status=status)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=[
            DeliveryOut(
                id=r.id,
                created_at=r.created_at,
                channel=NotificationChannel(r.channel),
                category=NotificationCategory(r.category),
                kind=r.kind,
                recipient=_mask(r.channel, r.recipient),
                subject=r.subject,
                body=r.body,
                status=DeliveryStatus(r.status),
                status_label=DELIVERY_STATUS_LABELS[DeliveryStatus(r.status)],
                is_critical=r.is_critical,
                deferred=r.deferred,
                scheduled_for=r.scheduled_for,
                sent_at=r.sent_at,
                provider=r.provider,
                cost_units=r.cost_units,
                is_overage=r.is_overage,
                is_fallback=r.fallback_for_id is not None,
                error=r.error,
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


# --- Platform: test gelen kutusu -----------------------------------------------------------


class TestInboxItemOut(BaseModel):
    id: int
    at: dt.datetime
    channel: str
    to: str
    subject: str | None
    body: str
    provider_ref: str


class TestInboxOut(BaseModel):
    items: list[TestInboxItemOut]
    note: str


@platform_router.get("/test-inbox", summary="Test sağlayıcısının son mesajları (yalnız demo)")
async def sent_test_messages(
    _: PlatformAdminDep,
    settings: SettingsDep,
    to: Annotated[str | None, Query(max_length=254)] = None,
) -> TestInboxOut:
    """Geliştirme/demo ortamında gönderilen kodları ve davet bağlantılarını görmek için.
    Üretimde (ya da `TEST_INBOX_ENABLED=false`) 404 — kodlar asla dışarı verilmez."""
    if not settings.test_inbox_active:
        raise NotFoundError
    target = to.strip().lower() if to else None
    return TestInboxOut(
        items=[
            TestInboxItemOut(
                id=i.id,
                at=i.at,
                channel=i.channel,
                to=i.to,
                subject=i.subject,
                body=i.body,
                provider_ref=i.provider_ref,
            )
            for i in TEST_INBOX.items(to=target)
        ],
        note="Test sağlayıcısı: bu mesajlar dışarı gönderilmedi (K5). Son 100 mesaj, sunucu "
        "belleğinde; yeniden başlatınca silinir.",
    )
