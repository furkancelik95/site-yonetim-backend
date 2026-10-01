"""Duyuru uçları — docs/06 §2.11 (modül: `announcements`; kapalıysa 404).

- Yayınlayan (`announcements.publish`) tüm duyuruları ve teslim sayılarını görür.
- Sakin yalnız kendisine teslim edilen, süresi geçmemiş duyuruları görür (docs/05 §6) ve okundu
  işaretleyebilir. Diğer personel yürürlükteki tüm duyuruları görür.
- Yayınlama teslim kayıtlarını yazar; gerçek gönderim yok (docs/12 K5).
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.requests import rule_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import Audience, Channel, Importance, OperationRuleError
from site_yonetim.models import Announcement
from site_yonetim.services import announcements as svc

router = APIRouter(prefix="/sites/{slug}/announcements", tags=["duyuru"])
AnnouncementsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.ANNOUNCEMENTS))]
Paging = Annotated[PageParams, Depends()]


def _read(ctx: SiteContext) -> None:
    if not ctx.access.can(Permission.ANNOUNCEMENTS_READ):
        raise ForbiddenError


def _publisher(ctx: SiteContext) -> bool:
    return ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH)


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    importance: Importance
    audience: Audience
    audience_block_ids: list[uuid.UUID]
    published_at: dt.datetime | None
    expires_on: dt.date | None
    published_by: str | None
    is_pinned: bool
    read_at: dt.datetime | None = Field(description="sakin için: okuduğu an; personelde null")
    recipient_count: int | None = Field(description="yalnız yayınlayana: hedef kişi sayısı")
    read_count: int | None = Field(description="yalnız yayınlayana: okuyan kişi sayısı")


async def out(ctx: SiteContext, rows: list[Announcement]) -> list[AnnouncementOut]:
    ids = [a.id for a in rows]
    stats = await svc.delivery_stats(ctx.session, ids) if _publisher(ctx) else {}
    person = ctx.access.person_id
    reads = await svc.read_times(ctx.session, person, ids) if person else {}
    publisher = _publisher(ctx)
    return [
        AnnouncementOut(
            id=a.id,
            title=a.title,
            body=a.body,
            importance=Importance(a.importance),
            audience=Audience(a.audience),
            audience_block_ids=a.audience_block_ids or [],
            published_at=a.published_at,
            expires_on=a.expires_on,
            published_by=a.published_by,
            is_pinned=a.is_pinned,
            read_at=reads.get(a.id),
            recipient_count=stats.get(a.id, (0, 0))[0] if publisher else None,
            read_count=stats.get(a.id, (0, 0))[1] if publisher else None,
        )
        for a in rows
    ]


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


@router.post("", status_code=status.HTTP_201_CREATED, summary="Duyuru yayınla")
async def publish_announcement(
    ctx: AnnouncementsModule,
    body: AnnouncementCreate,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AnnouncementOut]:
    if not _publisher(ctx):
        raise ForbiddenError
    try:
        published = await svc.publish(
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
            ),
            published_by=current.user.full_name,
            now=now,
            today=today,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [item] = await out(ctx, [published.announcement])
    message = f"Duyuru yayınlandı: {published.recipient_count} kişi uygulamada görecek."
    if len(published.channels) > 1:
        message += " E-posta/SMS/bildirim gönderimi henüz yok; teslim kayıtları bekliyor."
    return Written(data=item, message=message)


class ReadOut(BaseModel):
    read_at: dt.datetime


@router.post("/{announcement_id}/read", summary="Okundu işaretle (sakin)")
async def mark_read(
    announcement_id: uuid.UUID, ctx: AnnouncementsModule, now: NowDep, today: TodayDep
) -> ReadOut:
    person = ctx.access.person_id
    await _visible(ctx, announcement_id, today)
    read_at = await svc.mark_read(ctx.session, announcement_id, person, now) if person else None
    if read_at is None:
        raise NotFoundError("Bu duyuru size teslim edilmemiş.")
    await ctx.session.commit()
    return ReadOut(read_at=read_at)
