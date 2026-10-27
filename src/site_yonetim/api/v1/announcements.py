"""Duyuru uçları — docs/06 §2.11, servis isteği 71 (modül: `announcements`; kapalıysa 404).

- Yayınlayan (`announcements.publish`) tüm duyuruları, teslim/okuma/onay sayılarını, okuma
  raporunu ve zamanlanmış duyuruları görür.
- Sakin yalnız kendisine teslim edilen, süresi geçmemiş duyuruları görür (docs/05 §6), okundu
  işaretler ve okuma onayı ister duyuruda "Okudum, anladım" der. Diğer personel yürürlükteki
  tüm duyuruları görür.
- Yayınlama teslim kayıtlarını yazar; gerçek gönderim yok (docs/12 K5). `publish_at` ileride ise
  duyuru zamanlanır (sessiz saate düşerse bitişine kayar; kritik önem aşar).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.requests import rule_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import Audience, Channel, Importance, OperationRuleError
from site_yonetim.models import Announcement
from site_yonetim.services import announcements as svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.outbound import NOT_CONNECTED

router = APIRouter(prefix="/sites/{slug}/announcements", tags=["duyuru"])
scheduled_router = APIRouter(prefix="/sites/{slug}/scheduled-announcements", tags=["duyuru"])
resident_router = APIRouter(prefix="/sites/{slug}/resident", tags=["sakin"])
AnnouncementsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.ANNOUNCEMENTS))]
Paging = Annotated[PageParams, Depends()]


def _read(ctx: SiteContext) -> None:
    if not ctx.access.can(Permission.ANNOUNCEMENTS_READ):
        raise ForbiddenError


def _publisher(ctx: SiteContext) -> bool:
    return ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH)


async def _publisher_only(ctx: AnnouncementsModule) -> SiteContext:
    if not _publisher(ctx):
        raise ForbiddenError
    return ctx


Publisher = Annotated[SiteContext, Depends(_publisher_only)]


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    importance: Importance
    audience: Audience
    audience_block_ids: list[uuid.UUID]
    published_at: dt.datetime | None = Field(description="zamanlanmışsa null")
    scheduled_for: dt.datetime | None = Field(description="zamanlanmış yayın anı (kaydırılmış)")
    expires_on: dt.date | None
    published_by: str | None
    is_pinned: bool
    requires_ack: bool = Field(description="zorunlu okuma onayı")
    ack_deadline: dt.datetime | None = Field(description="son onay anı (yayın + ack_hours)")
    read_at: dt.datetime | None = Field(description="sakin için: okuduğu an; personelde null")
    acked_at: dt.datetime | None = Field(description="sakin için: onayladığı an")
    recipient_count: int | None = Field(description="yalnız yayınlayana: hedef kişi sayısı")
    read_count: int | None = Field(description="yalnız yayınlayana: okuyan kişi sayısı")
    ack_count: int | None = Field(description="yalnız yayınlayana ve onaylı duyuruda")


async def out(ctx: SiteContext, rows: list[Announcement]) -> list[AnnouncementOut]:
    ids = [a.id for a in rows]
    publisher = _publisher(ctx)
    stats = await svc.delivery_stats(ctx.session, ids) if publisher else {}
    person = ctx.access.person_id
    mine = await svc.my_deliveries(ctx.session, person, ids) if person else {}
    found = []
    for a in rows:
        total, read, acked = stats.get(a.id, (0, 0, 0))
        me = mine.get(a.id)
        found.append(
            AnnouncementOut(
                id=a.id,
                title=a.title,
                body=a.body,
                importance=Importance(a.importance),
                audience=Audience(a.audience),
                audience_block_ids=a.audience_block_ids or [],
                published_at=a.published_at,
                scheduled_for=a.scheduled_for,
                expires_on=a.expires_on,
                published_by=a.published_by,
                is_pinned=a.is_pinned,
                requires_ack=a.requires_ack,
                ack_deadline=a.ack_deadline,
                read_at=me.read_at if me else None,
                acked_at=me.acked_at if me else None,
                recipient_count=total if publisher else None,
                read_count=read if publisher else None,
                ack_count=acked if publisher and a.requires_ack else None,
            )
        )
    return found


@router.get("", summary="Duyurular (sabitlenmiş önce, sonra yeni)")
async def list_announcements(
    ctx: AnnouncementsModule, paging: Paging, today: TodayDep
) -> Page[AnnouncementOut]:
    _read(ctx)
    publisher = _publisher(ctx)
    query = svc.visible_query(
        show_all=publisher, person_id=None if publisher else ctx.access.person_id, today=today
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await out(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


async def _visible(ctx: SiteContext, announcement_id: uuid.UUID, today: dt.date) -> Announcement:
    _read(ctx)
    publisher = _publisher(ctx)
    query = svc.visible_query(
        show_all=publisher, person_id=None if publisher else ctx.access.person_id, today=today
    ).where(Announcement.id == announcement_id)
    row = await ctx.session.scalar(query)
    if row is None:
        raise NotFoundError("Duyuru bulunamadı.")
    return row


@router.get("/{announcement_id}", summary="Duyuru")
async def get_announcement(
    announcement_id: uuid.UUID, ctx: AnnouncementsModule, today: TodayDep
) -> AnnouncementOut:
    [item] = await out(ctx, [await _visible(ctx, announcement_id, today)])
    return item


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    importance: Importance = Importance.NORMAL
    audience: Audience = Audience.ALL_RESIDENTS
    audience_block_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    channels: list[Channel] = Field(
        default_factory=list, description="uygulama içi her zaman; diğerleri kayıt (K5)"
    )
    expires_on: dt.date | None = None
    is_pinned: bool = False
    requires_ack: bool = Field(default=False, description="zorunlu okuma onayı (16.3)")
    ack_hours: int | None = Field(default=None, description="son onay süresi, 1–720 saat")
    sms_fallback: bool = Field(
        default=False, description="süre dolunca onaylamayanlara bir kez SMS (P16c ile)"
    )
    publish_at: dt.datetime | None = Field(
        default=None, description="ileride ise zamanlanır; sessiz saatte bitişe kayar"
    )


def _local(value: dt.datetime) -> str:
    return value.astimezone(BUSINESS_TZ).strftime("%d.%m.%Y %H:%M")


@router.post("", status_code=status.HTTP_201_CREATED, summary="Duyuru yayınla ya da zamanla")
async def publish_announcement(
    ctx: Publisher,
    body: AnnouncementCreate,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AnnouncementOut]:
    if body.publish_at is not None and body.publish_at.tzinfo is None:
        body.publish_at = body.publish_at.replace(tzinfo=BUSINESS_TZ)
    try:
        result = await svc.create(
            ctx.session,
            svc.NewAnnouncement(
                title=body.title,
                body=body.body,
                importance=body.importance,
                audience=body.audience,
                block_ids=tuple(body.audience_block_ids),
                channels=tuple(body.channels),
                expires_on=body.expires_on,
                is_pinned=body.is_pinned,
                requires_ack=body.requires_ack,
                ack_hours=body.ack_hours,
                sms_fallback=body.sms_fallback,
                publish_at=body.publish_at,
            ),
            published_by=current.user.full_name,
            now=now,
            today=today,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    if isinstance(result, svc.Scheduled):
        await ctx.session.commit()
        [item] = await out(ctx, [result.announcement])
        at = result.announcement.scheduled_for or now
        message = f"Duyuru {_local(at)} için zamanlandı."
        if result.shifted:
            message += " Sessiz saatlere denk geldiği için sessiz saat bitişine kaydırıldı."
        return Written(data=item, message=message)
    quiet, quiet_to = await svc.in_quiet_now(ctx.session, now)
    await ctx.session.commit()
    [item] = await out(ctx, [result.announcement])
    message = f"Duyuru yayınlandı: {result.recipient_count} kişi uygulamada görecek."
    if body.requires_ack and body.ack_hours:
        message += f" Okuma onayı {body.ack_hours} saat içinde bekleniyor."
    if len(result.channels) > 1:
        message += " E-posta/SMS/bildirim gönderimi henüz yok; teslim kayıtları bekliyor."
        if quiet and body.importance is not Importance.CRITICAL:
            message += f" Sessiz saatler içinde: dış kanallar {quiet_to:%H:%M}'de gider."
    return Written(data=item, message=message)


class AnnouncementReadOut(BaseModel):
    read_at: dt.datetime


@router.post("/{announcement_id}/read", summary="Okundu işaretle (sakin)")
async def mark_read(
    announcement_id: uuid.UUID, ctx: AnnouncementsModule, now: NowDep, today: TodayDep
) -> AnnouncementReadOut:
    person = ctx.access.person_id
    await _visible(ctx, announcement_id, today)
    read_at = await svc.mark_read(ctx.session, announcement_id, person, now) if person else None
    if read_at is None:
        raise NotFoundError("Bu duyuru size teslim edilmemiş.")
    await ctx.session.commit()
    return AnnouncementReadOut(read_at=read_at)


# --- Okuma raporu (16.3) ------------------------------------------------------------------


class ReadReportRecipient(BaseModel):
    name: str
    units: list[str]
    delivered_at: dt.datetime | None
    read_at: dt.datetime | None
    acked_at: dt.datetime | None
    channel: Channel = Field(description="onay/okuma kanalı (şimdilik uygulama içi)")


class ReadReportAck(BaseModel):
    name: str
    units: list[str]
    at: dt.datetime
    channel: Channel


class SmsFallbackOut(BaseModel):
    due: bool = Field(description="son onay süresi doldu mu")
    count: int | None = Field(description="süre dolduğunda onaylamayan kişi; dolmadıysa null")
    sent: bool = Field(description="SMS gerçekten gönderildi mi (sağlayıcı yokken false)")
    note: str


class ReadReportOut(BaseModel):
    id: uuid.UUID
    title: str
    audience: Audience
    published_at: dt.datetime | None
    generated_at: dt.datetime = Field(description="rapor üretim zamanı (zaman damgası)")
    recipient_count: int
    read_count: int
    requires_ack: bool
    ack_deadline: dt.datetime | None
    acks: list[ReadReportAck] = Field(description="onaylayanlar, onay sırasına göre")
    not_acked: int | None
    sms_fallback: SmsFallbackOut | None
    recipients: list[ReadReportRecipient] = Field(description="alıcı listesi (kişi bazında)")


@router.get("/{announcement_id}/read-report", summary="Okuma raporu (alıcı, okuma, onay)")
async def read_report(
    announcement_id: uuid.UUID, ctx: Publisher, now: NowDep, today: TodayDep
) -> ReadReportOut:
    """Kişisel veri içerir (ad, bölüm, zaman): yalnız yayınlayan. Hane üyeleri teslim almadığı
    için listede yoktur (onayları bölüm adına sayılmaz)."""
    a = await svc.get(ctx.session, announcement_id)
    if a is None or a.published_at is None:
        raise NotFoundError("Duyuru bulunamadı.")
    rows = await svc.report_rows(ctx.session, a, today)
    acked = sorted((r for r in rows if r.acked_at), key=lambda r: r.acked_at or now)
    fallback = None
    if a.requires_ack and a.sms_fallback:
        ran = a.sms_fallback_ran_at is not None
        fallback = SmsFallbackOut(
            due=bool(a.ack_deadline and a.ack_deadline <= now),
            count=a.sms_fallback_count if ran else None,
            sent=bool(a.sms_fallback_sent),
            note=NOT_CONNECTED if not a.sms_fallback_sent else "SMS gönderildi.",
        )
    return ReadReportOut(
        id=a.id,
        title=a.title,
        audience=Audience(a.audience),
        published_at=a.published_at,
        generated_at=now,
        recipient_count=len(rows),
        read_count=sum(1 for r in rows if r.read_at),
        requires_ack=a.requires_ack,
        ack_deadline=a.ack_deadline,
        acks=[
            ReadReportAck(name=r.name, units=r.units, at=r.acked_at, channel=Channel.IN_APP)
            for r in acked
            if r.acked_at
        ],
        not_acked=len(rows) - len(acked) if a.requires_ack else None,
        sms_fallback=fallback,
        recipients=[
            ReadReportRecipient(
                name=r.name,
                units=r.units,
                delivered_at=r.delivered_at,
                read_at=r.read_at,
                acked_at=r.acked_at,
                channel=Channel.IN_APP,
            )
            for r in rows
        ],
    )


# --- Zamanlanmış duyurular ----------------------------------------------------------------


class ScheduledAnnouncementOut(BaseModel):
    id: uuid.UUID
    title: str
    importance: Importance
    publish_at: dt.datetime = Field(description="yayınlanacağı an (sessiz saatte kaydırılmış)")
    requested_at: dt.datetime = Field(description="istenen yayın anı")
    shifted: bool
    requires_ack: bool
    created_by: str | None


def _scheduled_out(a: Announcement) -> ScheduledAnnouncementOut:
    at = a.scheduled_for or a.created_at
    requested = a.requested_publish_at or at
    return ScheduledAnnouncementOut(
        id=a.id,
        title=a.title,
        importance=Importance(a.importance),
        publish_at=at,
        requested_at=requested,
        shifted=at != requested,
        requires_ack=a.requires_ack,
        created_by=a.published_by,
    )


@scheduled_router.get("", summary="Yayın bekleyen zamanlanmış duyurular")
async def scheduled(ctx: Publisher) -> list[ScheduledAnnouncementOut]:
    rows = await ctx.session.scalars(svc.scheduled_query().limit(200))
    return [_scheduled_out(a) for a in rows]


@scheduled_router.post("/{announcement_id}/cancel", summary="Zamanlanmış duyuruyu iptal et")
async def cancel_scheduled(
    announcement_id: uuid.UUID, ctx: Publisher, now: NowDep
) -> Written[ScheduledAnnouncementOut]:
    try:
        row = await svc.cancel(ctx.session, announcement_id, now)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = _scheduled_out(row)
    await ctx.session.commit()
    return Written(data=data, message="Zamanlanmış duyuru iptal edildi.")


# --- Sakin: okuma onayı -----------------------------------------------------------------


def _resident_person(ctx: SiteContext) -> uuid.UUID:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx.access.person_id


class PendingAckOut(BaseModel):
    announcement_id: uuid.UUID
    title: str
    deadline: dt.datetime | None


@resident_router.get("/pending-acks", summary="Okuma onayı bekleyen duyurularım")
async def pending_acks(ctx: AnnouncementsModule, today: TodayDep) -> list[PendingAckOut]:
    person = _resident_person(ctx)
    rows = await ctx.session.scalars(svc.pending_acks_query(person, today))
    return [
        PendingAckOut(announcement_id=a.id, title=a.title, deadline=a.ack_deadline) for a in rows
    ]


class AckOut(BaseModel):
    announcement_id: uuid.UUID
    acked_at: dt.datetime


@resident_router.post(
    "/announcements/{announcement_id}/acknowledge",
    status_code=status.HTTP_201_CREATED,
    summary='"Okudum, anladım" (zorunlu okuma onayı)',
)
async def acknowledge(
    announcement_id: uuid.UUID, ctx: AnnouncementsModule, now: NowDep, today: TodayDep
) -> Written[AckOut]:
    person = _resident_person(ctx)
    row = await ctx.session.scalar(
        svc.visible_query(show_all=False, person_id=person, today=today).where(
            Announcement.id == announcement_id
        )
    )
    if row is None:
        raise NotFoundError("Duyuru bulunamadı.")
    try:
        at = await svc.acknowledge(ctx.session, row, person, now)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except PermissionError as exc:  # pragma: no cover — görünür duyuru teslim edilmiştir
        raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=AckOut(announcement_id=row.id, acked_at=at), message="Okuma onayınız kaydedildi."
    )
