"""NotificationService — dış kanal gönderim katmanı (Yazılım Dokümanı PLT-05, ILT-05, §4.8).

Akış:
1. `services/notifications.notify` uygulama içi bildirimi yazar ve **aynı transaction'da**
   `plan` ile teslim kayıtlarını (push/SMS/e-posta) üretir: kişinin kanal tercihi, adresi,
   sessiz saat (acil kategori aşar) uygulanır. İşlem geri alınırsa teslim kaydı da alınır.
2. `dispatch_due` zamanı gelen kayıtları sağlayıcıya iletir (adaptör: `services/channels.py`),
   durumu, sağlayıcı referansını ve SMS adedini yazar; SMS sitenin aylık kotasından düşer,
   kota aşılırsa gönderim durmaz, "aşım" işaretlenir ve %80/%100'de yöneticiye uyarı gider.
   API isteğinden sonra hemen (`api/deps.get_session`), sessiz saat sonrası ve yeniden
   denemeler için dakikalık işle (`cli.py run-notification-jobs`) çalışır.
3. Kritik (acil) bildirimde push 2 dakika içinde teslim edilemezse SMS yedeği; push hiç
   gönderilemezse (jeton yok) yedek hemen.

Teslim kaydının metni gizli bilgi taşımaz (kodlar kimlik akışında, `services/otp.py`).
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from site_yonetim.db.tenancy import all_sites_scope, current_scope, site_scope
from site_yonetim.domain.delivery import (
    CRITICAL_CATEGORIES,
    CRITICAL_FALLBACK_AFTER,
    DEFAULT_QUIET_FROM,
    DEFAULT_QUIET_TO,
    DeliveryStatus,
    QuotaUsage,
    Reachability,
    SmsAccountMode,
    channels_for,
    send_time,
    sms_size,
    sms_text,
)
from site_yonetim.domain.notifications import (
    NotificationCategory,
    NotificationChannel,
    Overrides,
    preference_matrix,
)
from site_yonetim.models import (
    MessagingSettings,
    NotificationDelivery,
    NotificationPreference,
    PushToken,
    Site,
    User,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services.channels import Channels, OutboundMessage

logger = logging.getLogger(__name__)

DISPATCH_KEY = "delivery_dispatch_sites"
JOB_ACTOR = "Bildirim gönderim işi"
BATCH = 500
BUSINESS_TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True, slots=True)
class Planned:
    """Plan için gereken bildirim bilgisi (`notifications.Message`'ın dış kanal kısmı)."""

    category: NotificationCategory
    kind: str
    title: str
    body: str | None


# --- Ayarlar -------------------------------------------------------------------------


async def site_settings(session: AsyncSession) -> MessagingSettings | None:
    return await session.scalar(select(MessagingSettings))


def quiet_hours(row: MessagingSettings | None) -> tuple[time, time]:
    if row is None:
        return DEFAULT_QUIET_FROM, DEFAULT_QUIET_TO
    return row.quiet_from, row.quiet_to


def monthly_quota(row: MessagingSettings | None, default: int) -> int:
    if row is None or row.sms_monthly_quota is None:
        return default
    return row.sms_monthly_quota


def mark_dispatch(session: AsyncSession, site_id: uuid.UUID) -> None:
    sites: set[uuid.UUID] = session.info.setdefault(DISPATCH_KEY, set())
    sites.add(site_id)


def _site_id() -> uuid.UUID:
    scope = current_scope()
    if scope is None or scope.site_id is None:  # pragma: no cover - programlama hatası
        raise RuntimeError("Teslim kaydı site kapsamında yazılır.")
    return scope.site_id


# --- 1. Planlama ----------------------------------------------------------------------


async def _reachability(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Reachability, tuple[str, ...]]]:
    users = await session.execute(
        select(User.id, User.phone, User.email, User.is_active).where(User.id.in_(user_ids))
    )
    tokens: dict[uuid.UUID, list[str]] = {}
    for user_id, token in await session.execute(
        select(PushToken.user_id, PushToken.token).where(PushToken.user_id.in_(user_ids))
    ):
        tokens.setdefault(user_id, []).append(token)
    out = {}
    for user_id, phone, email, is_active in users:
        if not is_active:
            continue
        push = tuple(tokens.get(user_id, ()))
        out[user_id] = (Reachability(bool(push), phone, email), push)
    return out


async def _preferences(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Overrides]:
    rows = await session.scalars(
        select(NotificationPreference).where(NotificationPreference.user_id.in_(user_ids))
    )
    out: dict[uuid.UUID, Overrides] = {}
    for r in rows:
        out.setdefault(r.user_id, {})[
            (NotificationCategory(r.category), NotificationChannel(r.channel))
        ] = r.enabled
    return out


def _content(
    channel: NotificationChannel, site_name: str | None, item: Planned
) -> tuple[str | None, str]:
    """(konu, metin). SMS kısa: site adı + başlık (+ metin); e-posta konu + metin."""
    if channel is NotificationChannel.SMS:
        text = item.title if not item.body else f"{item.title} — {item.body}"
        return None, sms_text(site_name, text)[:480]
    if channel is NotificationChannel.EMAIL:
        subject = f"{site_name} · {item.title}" if site_name else item.title
        return subject, item.body or item.title
    return item.title, item.body or item.title


def _address(channel: NotificationChannel, reach: Reachability) -> str:
    if channel is NotificationChannel.SMS:
        return reach.phone or ""
    if channel is NotificationChannel.EMAIL:
        return reach.email or ""
    return "push"


async def plan(
    session: AsyncSession,
    recipients: Iterable[tuple[uuid.UUID, uuid.UUID]],
    item: Planned,
    *,
    now: datetime | None = None,
) -> int:
    """Uygulama içi bildirimi yeni yazılan kişiler için dış kanal teslim kayıtlarını üretir.

    `recipients`: (bildirim kimliği, kullanıcı kimliği). Tercihe göre kanal; adresi olmayan
    kanal atlanır. Döner: üretilen teslim kaydı sayısı."""
    pairs = list(recipients)
    if not pairs:
        return 0
    now = now or datetime.now(UTC)
    site_id = _site_id()
    user_ids = [u for _, u in pairs]
    reach = await _reachability(session, user_ids)
    prefs = await _preferences(session, user_ids)
    row = await site_settings(session)
    quiet_from, quiet_to = quiet_hours(row)
    site_name = await session.scalar(select(Site.name).where(Site.id == site_id))
    due = send_time(item.category, now, BUSINESS_TZ, quiet_from, quiet_to)
    critical = item.category in CRITICAL_CATEGORIES
    count = 0
    for notification_id, user_id in pairs:
        found = reach.get(user_id)
        if found is None:
            continue
        reachability, _tokens = found
        matrix = preference_matrix(prefs.get(user_id, {}))
        for channel in channels_for(item.category, matrix, reachability):
            subject, body = _content(channel, site_name, item)
            session.add(
                NotificationDelivery(
                    notification_id=notification_id,
                    user_id=user_id,
                    channel=channel.value,
                    category=item.category.value,
                    kind=item.kind,
                    recipient=_address(channel, reachability),
                    subject=subject,
                    body=body,
                    status=DeliveryStatus.QUEUED.value,
                    is_critical=critical,
                    scheduled_for=due,
                    deferred=due > now,
                )
            )
            count += 1
    if count:
        await session.flush()
        mark_dispatch(session, site_id)
    return count


async def queue_direct(
    session: AsyncSession,
    *,
    channel: NotificationChannel,
    to: str,
    body: str,
    category: NotificationCategory,
    kind: str,
    user_id: uuid.UUID | None = None,
    subject: str | None = None,
    now: datetime | None = None,
    respect_quiet_hours: bool = False,
) -> NotificationDelivery:
    """Kişiye bağlı olmayan ya da bildirim merkezine yazılmayan tek mesaj (ör. başvuru onayı
    SMS'i, test SMS'i). SMS başına site adı eklenir."""
    now = now or datetime.now(UTC)
    site_id = _site_id()
    due = now
    if respect_quiet_hours:
        quiet_from, quiet_to = quiet_hours(await site_settings(session))
        due = send_time(category, now, BUSINESS_TZ, quiet_from, quiet_to)
    if channel is NotificationChannel.SMS:
        site_name = await session.scalar(select(Site.name).where(Site.id == site_id))
        body = sms_text(site_name, body)
    delivery = NotificationDelivery(
        user_id=user_id,
        channel=channel.value,
        category=category.value,
        kind=kind,
        recipient=to,
        subject=subject,
        body=body,
        status=DeliveryStatus.QUEUED.value,
        is_critical=category in CRITICAL_CATEGORIES,
        scheduled_for=due,
        deferred=due > now,
    )
    session.add(delivery)
    await session.flush()
    mark_dispatch(session, site_id)
    return delivery


# --- 2. Gönderim ----------------------------------------------------------------------


def month_bounds(today: date) -> tuple[datetime, datetime]:
    start = datetime(today.year, today.month, 1, tzinfo=BUSINESS_TZ)
    end_month = (today.month % 12) + 1
    end_year = today.year + (1 if today.month == 12 else 0)
    end = datetime(end_year, end_month, 1, tzinfo=BUSINESS_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


async def sms_used(session: AsyncSession, *, start: datetime, end: datetime) -> int:
    used = await session.scalar(
        select(func.coalesce(func.sum(NotificationDelivery.cost_units), 0)).where(
            NotificationDelivery.channel == NotificationChannel.SMS.value,
            NotificationDelivery.sent_at >= start,
            NotificationDelivery.sent_at < end,
        )
    )
    return int(used or 0)


async def _quota_warning(
    session: AsyncSession, site: Site, usage: QuotaUsage, percent: int, month: str
) -> None:
    from site_yonetim.domain.access import Permission
    from site_yonetim.services import notifications as notifications_svc
    from site_yonetim.services import roles as roles_svc

    managers = await roles_svc.permission_holders(session, site, Permission.NOTIFICATIONS_MANAGE)
    title = (
        "SMS kotası doldu; gönderim sürüyor, aşım faturaya yansır"
        if percent >= 100
        else f"SMS kotasının %{percent}'i kullanıldı"
    )
    await notifications_svc.notify(
        session,
        managers,
        notifications_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="sms.quota",
            title=title,
            body=f"Bu ay {usage.used} / {usage.quota} SMS kullanıldı.",
            link_entity="sms_account",
        ),
        dedupe_key=f"sms-quota:{month}:{percent}",
    )


async def _push_tokens(session: AsyncSession, user_id: uuid.UUID | None) -> tuple[str, ...]:
    if user_id is None:
        return ()
    return tuple(await session.scalars(select(PushToken.token).where(PushToken.user_id == user_id)))


async def _fallback_sms(session: AsyncSession, push: NotificationDelivery, now: datetime) -> bool:
    """Kritik push için SMS yedeği (bildirim başına bir SMS)."""
    if push.notification_id is not None:
        has_sms = await session.scalar(
            select(
                exists().where(
                    NotificationDelivery.notification_id == push.notification_id,
                    NotificationDelivery.channel == NotificationChannel.SMS.value,
                )
            )
        )
        if has_sms:
            return False
    phone = (
        await session.scalar(select(User.phone).where(User.id == push.user_id))
        if push.user_id
        else None
    )
    if not phone:
        return False
    site_name = await session.scalar(select(Site.name).where(Site.id == push.site_id))
    title = push.subject or push.body
    text = title if not push.body or push.body == title else f"{title} — {push.body}"
    session.add(
        NotificationDelivery(
            notification_id=push.notification_id,
            user_id=push.user_id,
            channel=NotificationChannel.SMS.value,
            category=push.category,
            kind=push.kind,
            recipient=phone,
            subject=None,
            body=sms_text(site_name, text or "")[:480],
            status=DeliveryStatus.QUEUED.value,
            is_critical=True,
            scheduled_for=now,
            fallback_for_id=push.id,
        )
    )
    await session.flush()
    return True


@dataclass(frozen=True, slots=True)
class DispatchResult:
    sent: int = 0
    failed: int = 0
    fallbacks: int = 0


async def _dispatch_site(
    session: AsyncSession,
    site: Site,
    channels: Channels,
    *,
    now: datetime,
    default_quota: int,
) -> DispatchResult:
    sent = failed = fallbacks = 0
    row = await site_settings(session)
    sender = row.sms_sender if row and row.sms_mode == SmsAccountMode.OWN.value else None
    quota = monthly_quota(row, default_quota)
    today = now.astimezone(BUSINESS_TZ).date()
    start, end = month_bounds(today)
    month = f"{today:%Y-%m}"
    for _round in range(2):  # 2. tur: aynı çalışmada üretilen SMS yedekleri
        due = list(
            await session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == DeliveryStatus.QUEUED.value,
                    NotificationDelivery.scheduled_for <= now,
                )
                .order_by(NotificationDelivery.scheduled_for, NotificationDelivery.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        if not due:
            break
        for d in due:
            channel = NotificationChannel(d.channel)
            message = OutboundMessage(
                channel=channel,
                to=d.recipient,
                body=d.body,
                subject=d.subject,
                sender=sender if channel is NotificationChannel.SMS else None,
                push_tokens=await _push_tokens(session, d.user_id)
                if channel is NotificationChannel.PUSH
                else (),
            )
            try:
                result = await channels.for_channel(channel).send(message, now=now)
            except Exception as exc:  # sağlayıcı hatası bir kaydı durdurur, işi değil
                logger.warning("Gönderim hatası (%s): %s", d.id, exc)
                d.status = DeliveryStatus.FAILED.value
                d.error = "Sağlayıcı hatası; tekrar denenmedi."
                failed += 1
                continue
            d.provider = result.provider
            d.provider_ref = result.provider_ref
            d.status = result.status.value
            d.error = result.error
            if result.status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED):
                d.sent_at = now
                sent += 1
                if result.status is DeliveryStatus.DELIVERED:
                    d.delivered_at = now
                if channel is NotificationChannel.SMS:
                    before = await sms_used(session, start=start, end=end)
                    d.cost_units = result.cost_units
                    usage = QuotaUsage(quota, before + result.cost_units)
                    d.is_overage = usage.used > quota
                    await session.flush()
                    for percent in usage.crossed(before):
                        await _quota_warning(session, site, usage, percent, month)
            else:
                failed += 1
                if d.is_critical and channel is NotificationChannel.PUSH:
                    fallbacks += int(await _fallback_sms(session, d, now))
        await session.flush()
    return DispatchResult(sent, failed, fallbacks)


async def dispatch_due(
    factory: async_sessionmaker[AsyncSession],
    channels: Channels,
    *,
    now: datetime,
    default_quota: int,
    site_ids: Iterable[uuid.UUID] | None = None,
) -> list[tuple[uuid.UUID, DispatchResult]]:
    """Zamanı gelen teslimleri gönderir; site başına ayrı transaction (bir sitedeki hata
    diğerini durdurmaz). Tekrar güvenli: satır kilidi + durum geçişi."""
    if site_ids is None:
        with all_sites_scope():
            async with factory() as session:
                ids = sorted(
                    set(
                        await session.scalars(
                            select(NotificationDelivery.site_id).where(
                                NotificationDelivery.status == DeliveryStatus.QUEUED.value,
                                NotificationDelivery.scheduled_for <= now,
                            )
                        )
                    )
                )
    else:
        ids = sorted(set(site_ids))
    results: list[tuple[uuid.UUID, DispatchResult]] = []
    for site_id in ids:
        try:
            with site_scope(site_id):
                async with factory() as session, session.begin():
                    site = await session.get(Site, site_id)
                    if site is None:  # pragma: no cover
                        continue
                    results.append(
                        (
                            site_id,
                            await _dispatch_site(
                                session, site, channels, now=now, default_quota=default_quota
                            ),
                        )
                    )
        except Exception:  # pragma: no cover - bir sitenin hatası diğerlerini durdurmaz
            logger.exception("Bildirim gönderimi başarısız: site=%s", site_id)
    return results


async def run_critical_fallbacks(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Kritik push 2 dakikada teslim edilmediyse SMS yedeği üretir (dakikalık iş)."""
    created = 0
    cutoff = now - CRITICAL_FALLBACK_AFTER
    sms = aliased(NotificationDelivery)
    stale = select(NotificationDelivery).where(
        NotificationDelivery.is_critical,
        NotificationDelivery.channel == NotificationChannel.PUSH.value,
        NotificationDelivery.status == DeliveryStatus.SENT.value,
        NotificationDelivery.sent_at <= cutoff,
        ~select(sms.id)
        .where(
            sms.site_id == NotificationDelivery.site_id,
            sms.notification_id == NotificationDelivery.notification_id,
            sms.channel == NotificationChannel.SMS.value,
        )
        .correlate(NotificationDelivery)
        .exists(),
    )
    with all_sites_scope():
        async with factory() as session:
            site_ids = sorted(set(await session.scalars(stale.with_only_columns(
                NotificationDelivery.site_id))))  # fmt: skip
    set_actor(Actor(None, JOB_ACTOR, None))
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                for push in list(await session.scalars(stale.limit(BATCH))):
                    created += int(await _fallback_sms(session, push, now))
    return created


# --- Okuma: kota ve teslim geçmişi ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonthUsage:
    month: str
    usage: QuotaUsage
    forecast: int
    sms_count: int
    overage_messages: int
    by_category: dict[str, int]
    queued: int


async def month_usage(session: AsyncSession, *, now: datetime, default_quota: int) -> MonthUsage:
    today = now.astimezone(BUSINESS_TZ).date()
    start, end = month_bounds(today)
    row = await site_settings(session)
    quota = monthly_quota(row, default_quota)
    in_month = and_(
        NotificationDelivery.channel == NotificationChannel.SMS.value,
        NotificationDelivery.sent_at >= start,
        NotificationDelivery.sent_at < end,
    )
    by_category: dict[str, int] = {}
    count = overage = used = 0
    for category, units, n, over in await session.execute(
        select(
            NotificationDelivery.category,
            func.coalesce(func.sum(NotificationDelivery.cost_units), 0),
            func.count(),
            func.count().filter(NotificationDelivery.is_overage),
        )
        .where(in_month)
        .group_by(NotificationDelivery.category)
    ):
        by_category[category] = int(units)
        used += int(units)
        count += int(n)
        overage += int(over)
    queued = await session.scalar(
        select(func.count()).where(
            NotificationDelivery.status == DeliveryStatus.QUEUED.value,
        )
    )
    usage = QuotaUsage(quota, used)
    days = (end.astimezone(BUSINESS_TZ).date() - start.astimezone(BUSINESS_TZ).date()).days
    return MonthUsage(
        month=f"{today:%Y-%m}",
        usage=usage,
        forecast=usage.forecast(today.day, days),
        sms_count=count,
        overage_messages=overage,
        by_category=by_category,
        queued=int(queued or 0),
    )


def deliveries_query(
    *, channel: NotificationChannel | None = None, status: DeliveryStatus | None = None
) -> Any:
    query = select(NotificationDelivery)
    if channel is not None:
        query = query.where(NotificationDelivery.channel == channel.value)
    if status is not None:
        query = query.where(NotificationDelivery.status == status.value)
    return query.order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())


def sms_estimate(text: str, site_name: str | None) -> tuple[str, int]:
    size = sms_size(sms_text(site_name, text))
    return size.encoding, size.segments
