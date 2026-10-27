"""Bildirim merkezi, tercihler ve push jetonları — Yazılım Dokümanı PLT-05 (docs/06 §2.21).

- Bildirimler siteye aittir: `/sites/{slug}/notifications` yalnız oturumdaki kişinin o sitedeki
  bildirimleridir (başkasınınki "yok"tur). Siteye erişimi olan her üye (sakin dahil) kullanır.
- Tercihler ve push jetonları kişiye aittir: `/me/…`.
- Dış gönderim (push/SMS/e-posta) açık karar K5'e kadar yok; tercih şimdiden saklanır.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SessionDep,
    SiteContextDep,
    UserAccessDep,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.notifications import (
    CATEGORIES,
    Matrix,
    NotificationCategory,
    NotificationChannel,
    PreferenceError,
    PushPlatform,
    is_locked,
)
from site_yonetim.models import Notification
from site_yonetim.services import notifications as svc

router = APIRouter(prefix="/sites/{slug}/notifications", tags=["bildirim"])
me_router = APIRouter(prefix="/me", tags=["bildirim"])
Paging = Annotated[PageParams, Depends()]


class NotificationOut(BaseModel):
    id: uuid.UUID
    category: NotificationCategory
    kind: str = Field(description='olay türü, ör. "approval.requested" — ikon seçimi için')
    title: str
    body: str | None
    link_entity: str | None = Field(description='tıklayınca: kayıt türü, ör. "approval_requests"')
    link_id: uuid.UUID | None
    created_at: dt.datetime
    read_at: dt.datetime | None

    @classmethod
    def of(cls, n: Notification) -> NotificationOut:
        return cls(
            id=n.id,
            category=NotificationCategory(n.category),
            kind=n.kind,
            title=n.title,
            body=n.body,
            link_entity=n.link_entity,
            link_id=n.link_id,
            created_at=n.created_at,
            read_at=n.read_at,
        )


class NotificationPage(Page[NotificationOut]):
    unread_count: int


class ReadIn(BaseModel):
    ids: list[uuid.UUID] | None = Field(
        default=None, max_length=500, description="boşsa okunmamışların hepsi"
    )


class NotificationReadOut(BaseModel):
    marked: int
    unread_count: int


@router.get("", summary="Bildirimlerim (son 90 gün, yeni üstte)")
async def list_notifications(
    ctx: SiteContextDep,
    current: CurrentUserDep,
    paging: Paging,
    now: NowDep,
    unread: Annotated[bool, Query(description="true → yalnız okunmamış")] = False,
    category: NotificationCategory | None = None,
) -> NotificationPage:
    query = svc.list_query(current.user.id, now=now, unread=unread, category=category)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size))
    return NotificationPage(
        items=[NotificationOut.of(n) for n in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
        unread_count=await svc.unread_count(ctx.session, current.user.id, now=now),
    )


@router.get("/unread-count", summary="Okunmamış sayısı (zil rozeti)")
async def unread_count(ctx: SiteContextDep, current: CurrentUserDep, now: NowDep) -> int:
    return await svc.unread_count(ctx.session, current.user.id, now=now)


@router.post("/read", summary="Okundu işaretle (seçilenler ya da tümü)")
async def mark_read(
    body: ReadIn, ctx: SiteContextDep, current: CurrentUserDep, now: NowDep
) -> Written[NotificationReadOut]:
    marked = await svc.mark_read(ctx.session, current.user.id, now=now, ids=body.ids)
    await ctx.session.commit()
    remaining = await svc.unread_count(ctx.session, current.user.id, now=now)
    message = (
        "Tüm bildirimler okundu olarak işaretlendi."
        if body.ids is None
        else f"{marked} bildirim okundu olarak işaretlendi."
    )
    return Written(data=NotificationReadOut(marked=marked, unread_count=remaining), message=message)


# --- Kişiye ait: siteler arası özet, tercihler, push jetonları ----------------------------


class SiteUnreadOut(BaseModel):
    site_id: uuid.UUID
    slug: str
    unread_count: int


@me_router.get("/notifications/summary", summary="Sitelere göre okunmamış bildirim sayısı")
async def unread_summary(
    current: CurrentUserDep, access: UserAccessDep, factory: FactoryDep, now: NowDep
) -> list[SiteUnreadOut]:
    sites = {s.site_id: s.slug for s in access.sites}
    counts = await svc.unread_by_site(factory, current.user.id, list(sites), now=now)
    return [
        SiteUnreadOut(site_id=site_id, slug=slug, unread_count=counts.get(site_id, 0))
        for site_id, slug in sites.items()
    ]


class PreferenceCell(BaseModel):
    channel: NotificationChannel
    enabled: bool
    locked: bool = Field(description="true → kapatılamaz (kilit ikonu)")


class PreferenceRow(BaseModel):
    category: NotificationCategory
    label: str
    rule: str = Field(description="locked · at_least_one (en az bir kanal açık) · free")
    channels: list[PreferenceCell]


class PreferencesOut(BaseModel):
    in_app: bool = Field(default=True, description="uygulama içi bildirim her zaman açık")
    delivery_enabled: bool = Field(
        default=True,
        description="true: push/SMS/e-posta gönderim katmanı tercihleri uygular",
    )
    test_provider: bool = Field(
        default=True,
        description="true: sağlayıcı seçilene dek (K5) test sağlayıcısı — gönderim kayda "
        "geçer, dışarı gitmez",
    )
    categories: list[PreferenceRow]


class PreferenceChange(BaseModel):
    category: NotificationCategory
    channel: NotificationChannel
    enabled: bool


class PreferencesIn(BaseModel):
    changes: list[PreferenceChange] = Field(min_length=1, max_length=50)


def _preferences_out(matrix: Matrix) -> PreferencesOut:
    return PreferencesOut(
        categories=[
            PreferenceRow(
                category=category,
                label=CATEGORIES[category].label,
                rule=CATEGORIES[category].rule.value,
                channels=[
                    PreferenceCell(
                        channel=channel, enabled=enabled, locked=is_locked(category, channel)
                    )
                    for channel, enabled in channels.items()
                ],
            )
            for category, channels in matrix.items()
        ]
    )


@me_router.get("/notification-preferences", summary="Bildirim tercihlerim (kategori × kanal)")
async def get_preferences(current: CurrentUserDep, session: SessionDep) -> PreferencesOut:
    return _preferences_out(await svc.preferences(session, current.user.id))


@me_router.put("/notification-preferences", summary="Bildirim tercihlerimi değiştir")
async def put_preferences(
    body: PreferencesIn, current: CurrentUserDep, session: SessionDep
) -> Written[PreferencesOut]:
    changes = {(c.category, c.channel): c.enabled for c in body.changes}
    try:
        matrix = await svc.update_preferences(session, current.user.id, changes)
    except PreferenceError as exc:
        await session.rollback()
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "preference_not_allowed", str(exc)) from exc
    await session.commit()
    return Written(data=_preferences_out(matrix), message="Bildirim tercihleriniz kaydedildi.")


class PushTokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=4096)
    platform: PushPlatform


@me_router.post(
    "/push-tokens", status_code=status.HTTP_204_NO_CONTENT, summary="Cihaz push jetonunu kaydet"
)
async def register_push_token(
    body: PushTokenIn, current: CurrentUserDep, session: SessionDep, now: NowDep
) -> Response:
    """Uygulama açılışında ve jeton yenilenince çağrılır (tekrar güvenli)."""
    await svc.register_push_token(
        session, current.user.id, token=body.token, platform=body.platform, now=now
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class PushTokenRemoveIn(BaseModel):
    token: str = Field(min_length=10, max_length=4096)


@me_router.post(
    "/push-tokens/remove",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cihaz push jetonunu sil (çıkışta)",
)
async def remove_push_token(
    body: PushTokenRemoveIn, current: CurrentUserDep, session: SessionDep
) -> Response:
    if not await svc.remove_push_token(session, current.user.id, body.token):
        raise NotFoundError("Jeton bulunamadı.")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
