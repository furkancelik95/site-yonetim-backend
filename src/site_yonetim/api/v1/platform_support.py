"""Platform destek — platform konsolu ve bilgi bankası (Yazılım Dokümanı PYN-04, PYN-06;
servis istekleri 82–84). **Platform uçları yalnız platform yöneticisine; başkası 404.**

- Biletler ve destek oturumları kiracı verisidir: liste bilinçli `all_sites_scope()` ile okunur;
  bir siteye yazarken o sitenin kapsamında yeni oturum açılır.
- Destek ekibi müşteri verisini burada görmez; yalnız bilet yazışması ve oturum talebi vardır.
  Siteye erişim yalnız tesis yöneticisinin onayladığı süreli, salt okunur oturumla (K12).
- Bilgi bankası platform içeriğidir (tüm tesisler için ortak): okuma her oturum açmış kullanıcı,
  yazma platform yöneticisi.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, or_, select

from site_yonetim.api.deps import CurrentUserDep, FactoryDep, NowDep, PlatformAdminDep, SessionDep
from site_yonetim.api.schemas import MAX_PAGE_SIZE, Page, PageParams, Written
from site_yonetim.api.v1.support import (
    SupportSessionOut,
    SupportTicketOut,
    SupportTicketPage,
    TicketMessageIn,
    TicketSummaryOut,
    rule_error,
    session_out,
    ticket_out,
)
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.support import (
    ARTICLE_BODY_MAX,
    ARTICLE_BODY_MIN,
    ARTICLE_TITLE_MAX,
    ARTICLE_TITLE_MIN,
    REASON_MAX,
    REASON_MIN,
    SCREEN_MAX,
    SESSION_HOURS_MAX,
    SESSION_HOURS_MIN,
    ArticleAudience,
    TicketStatus,
    clean_screens,
)
from site_yonetim.models import KnowledgeArticle, Site, SupportSession, SupportTicket, User
from site_yonetim.services import support as svc
from site_yonetim.services.sites import find_site_by_slug

router = APIRouter(prefix="/platform", tags=["platform destek"])
knowledge_router = APIRouter(prefix="/knowledge-articles", tags=["bilgi bankası"])
Paging = Annotated[PageParams, Depends()]


# --- Biletler (82) -----------------------------------------------------------------


class SupportReplyIn(TicketMessageIn):
    status: Literal["answered", "waiting_customer", "resolved"] = "answered"


_PRIORITY_ORDER = case(
    (SupportTicket.priority == "critical", 0), (SupportTicket.priority == "high", 1), else_=2
)


@router.get("/support-tickets", summary="Tüm tesislerin destek biletleri")
async def platform_tickets(
    _: PlatformAdminDep,
    session: SessionDep,
    paging: Paging,
    now: NowDep,
    status_: Annotated[TicketStatus | None, Query(alias="status")] = None,
) -> SupportTicketPage:
    """Öncelik (kritik önce), sonra en yeni. SLA aşımı yanıtta hesaplanır; `summary` tüm
    tesislerin açık biletlerini veritabanında sayar (sayfa ve süzgeçten bağımsız)."""
    query = select(SupportTicket, Site).join(Site, Site.id == SupportTicket.site_id)
    if status_ is not None:
        query = query.where(SupportTicket.status == status_.value)
    with all_sites_scope():
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await session.execute(
                query.order_by(_PRIORITY_ORDER, SupportTicket.created_at.desc())
                .offset(paging.offset)
                .limit(paging.page_size)
            )
        ).all()
        messages = await svc.messages_of(session, [t.id for t, _ in rows])
        summary = await svc.sla_summary(session, now)
    return SupportTicketPage(
        items=[ticket_out(t, s, messages[t.id], now) for t, s in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
        summary=TicketSummaryOut.of(summary),
    )


async def _ticket_site(session: SessionDep, ticket_id: uuid.UUID) -> Site:
    with all_sites_scope():
        site = await session.scalar(
            select(Site)
            .join(SupportTicket, SupportTicket.site_id == Site.id)
            .where(SupportTicket.id == ticket_id)
        )
    if site is None:
        raise NotFoundError("Bilet bulunamadı.")
    return site


@router.post("/support-tickets/{ticket_id}/messages", summary="Bileti yanıtla (durumla)")
async def reply_ticket(
    ticket_id: uuid.UUID,
    body: SupportReplyIn,
    current: PlatformAdminDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> Written[SupportTicketOut]:
    site = await _ticket_site(session, ticket_id)
    async with svc.in_site(factory, site.id) as site_session:
        ticket = await svc.get_ticket(site_session, ticket_id)
        if ticket is None:  # pragma: no cover - az önce bulundu
            raise NotFoundError("Bilet bulunamadı.")
        try:
            await svc.support_reply(
                site_session,
                ticket,
                user_id=current.user.id,
                user_name=current.user.full_name,
                body=body.body,
                status=TicketStatus(body.status),
                now=now,
            )
        except svc.SupportRuleError as exc:
            raise rule_error(exc) from exc
        await svc.notify_ticket_answer(site_session, ticket)
        messages = await svc.messages_of(site_session, [ticket.id])
        out = ticket_out(ticket, site, messages[ticket.id], now)
        await site_session.commit()
    return Written(data=out, message="Yanıt gönderildi.")


# --- Destek oturumu (84) -------------------------------------------------------------


class SupportSessionIn(BaseModel):
    site_slug: str = Field(min_length=1, max_length=60)
    site_name: str | None = Field(default=None, description="yok sayılır; ad siteden okunur")
    reason: str = Field(min_length=REASON_MIN, max_length=REASON_MAX)
    hours: int = Field(ge=SESSION_HOURS_MIN, le=SESSION_HOURS_MAX)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


@router.post(
    "/support-sessions",
    status_code=status.HTTP_201_CREATED,
    summary="Destek oturumu talebi (tesis yöneticisinin onayına)",
)
async def request_session(
    body: SupportSessionIn,
    current: PlatformAdminDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> Written[SupportSessionOut]:
    site = await find_site_by_slug(session, body.site_slug)
    if site is None:
        message = "Tesis bulunamadı."
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", message, {"site_slug": message}
        )
    async with svc.in_site(factory, site.id) as site_session:
        try:
            row = await svc.request_session(
                site_session,
                site,
                user_id=current.user.id,
                user_name=current.user.full_name,
                reason=body.reason,
                hours=body.hours,
                now=now,
            )
        except svc.SupportRuleError as exc:
            raise rule_error(exc) from exc
        out = session_out(row, site, now)
        await site_session.commit()
    return Written(data=out, message="Oturum talebi tesis yöneticisinin onayına gönderildi.")


@router.get("/support-sessions", summary="Destek oturumları (tüm tesisler)")
async def platform_sessions(
    _: PlatformAdminDep, session: SessionDep, paging: Paging, now: NowDep
) -> Page[SupportSessionOut]:
    with all_sites_scope():
        total = await session.scalar(select(func.count(SupportSession.id)))
        rows = (
            await session.execute(
                select(SupportSession, Site)
                .join(Site, Site.id == SupportSession.site_id)
                .order_by(SupportSession.created_at.desc(), SupportSession.id.desc())
                .offset(paging.offset)
                .limit(paging.page_size)
            )
        ).all()
    return Page(
        items=[session_out(r, s, now) for r, s in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


# --- Bilgi bankası (83) --------------------------------------------------------------


class KnowledgeArticleOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    audience: ArticleAudience
    screens: list[str]
    helpful: int
    not_helpful: int
    updated_at: dt.datetime
    updated_by: str
    archived_at: dt.datetime | None = Field(
        default=None, description="yayından kaldırıldı (arşiv); yardımda ve aramada görünmez"
    )


class KnowledgeArticleIn(BaseModel):
    title: str = Field(min_length=ARTICLE_TITLE_MIN, max_length=ARTICLE_TITLE_MAX)
    body: str = Field(min_length=ARTICLE_BODY_MIN, max_length=ARTICLE_BODY_MAX)
    audience: ArticleAudience
    screens: list[Annotated[str, Field(max_length=SCREEN_MAX)]] = Field(default_factory=list)

    @field_validator("title", "body", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class KnowledgeArticlePatch(BaseModel):
    title: str | None = Field(
        default=None, min_length=ARTICLE_TITLE_MIN, max_length=ARTICLE_TITLE_MAX
    )
    body: str | None = Field(default=None, min_length=ARTICLE_BODY_MIN, max_length=ARTICLE_BODY_MAX)
    audience: ArticleAudience | None = None
    screens: list[Annotated[str, Field(max_length=SCREEN_MAX)]] | None = None

    @field_validator("title", "body", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class FeedbackIn(BaseModel):
    helpful: bool


def article_out(article: KnowledgeArticle, counts: tuple[int, int]) -> KnowledgeArticleOut:
    stamp = article.updated_at or article.created_at
    return KnowledgeArticleOut(
        id=article.id,
        title=article.title,
        body=article.body,
        audience=ArticleAudience(article.audience),
        screens=list(article.screens or []),
        helpful=counts[0],
        not_helpful=counts[1],
        updated_at=stamp,
        updated_by=article.updated_by_name,
        archived_at=article.archived_at,
    )


async def _article_out(session: SessionDep, article: KnowledgeArticle) -> KnowledgeArticleOut:
    counts = await svc.feedback_counts(session, [article.id])
    return article_out(article, counts.get(article.id, (0, 0)))


@knowledge_router.get("", summary="Bilgi bankası (arama, ekran, okuyucu)")
async def list_articles(
    _: CurrentUserDep,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    screen: Annotated[str | None, Query(max_length=200)] = None,
    audience: ArticleAudience | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
) -> Page[KnowledgeArticleOut]:
    """Makale tüm tesisler için ortak. `screen` yol önekiyle eşleşir (`satin-alma` makalesi
    `satin-alma/talepler` ekranında da görünür). Yayından kaldırılan (arşiv) makale görünmez."""
    return await _article_page(
        session, q=q, screen=screen, audience=audience, archived=False, page=page,
        page_size=page_size,
    )  # fmt: skip


async def _article_page(
    session: SessionDep,
    *,
    q: str | None,
    screen: str | None,
    audience: ArticleAudience | None,
    archived: bool,
    page: int,
    page_size: int,
) -> Page[KnowledgeArticleOut]:
    query = select(KnowledgeArticle).where(
        KnowledgeArticle.archived_at.is_not(None)
        if archived
        else KnowledgeArticle.archived_at.is_(None)
    )
    if audience is not None:
        query = query.where(KnowledgeArticle.audience == audience.value)
    if q and q.strip():
        needle = "%" + q.strip().replace("\\", "").replace("%", "").replace("_", "") + "%"
        query = query.where(
            or_(KnowledgeArticle.title.ilike(needle), KnowledgeArticle.body.ilike(needle))
        )
    if screen and screen.strip():
        # Önek eşleşmesi: ekran yolunun her öneki makale ekranlarından biri mi?
        parts = [p for p in screen.strip().strip("/").split("/") if p]
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        query = query.where(KnowledgeArticle.screens.overlap(prefixes))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        await session.scalars(
            query.order_by(
                KnowledgeArticle.updated_at.desc().nulls_last(), KnowledgeArticle.created_at.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    counts = await svc.feedback_counts(session, [a.id for a in rows])
    return Page(
        items=[article_out(a, counts.get(a.id, (0, 0))) for a in rows],
        page=page,
        page_size=page_size,
        total=total or 0,
    )


@knowledge_router.post("/{article_id}/feedback", summary='"Faydalı mı?" geri bildirimi')
async def article_feedback(
    article_id: uuid.UUID, body: FeedbackIn, current: CurrentUserDep, session: SessionDep
) -> Written[KnowledgeArticleOut]:
    article = await session.get(KnowledgeArticle, article_id)
    if article is None or article.archived_at is not None:
        raise NotFoundError("Makale bulunamadı.")
    await svc.give_feedback(session, article, user_id=current.user.id, helpful=body.helpful)
    out = await _article_out(session, article)
    await session.commit()
    return Written(data=out, message="Geri bildiriminiz için teşekkürler.")


@router.post("/knowledge-articles", status_code=status.HTTP_201_CREATED, summary="Makale yayınla")
async def create_article(
    body: KnowledgeArticleIn, current: PlatformAdminDep, session: SessionDep
) -> Written[KnowledgeArticleOut]:
    article = KnowledgeArticle(
        title=body.title,
        body=body.body,
        audience=body.audience.value,
        screens=clean_screens(body.screens),
        updated_by_id=current.user.id,
        updated_by_name=current.user.full_name,
    )
    session.add(article)
    await session.flush()
    out = article_out(article, (0, 0))
    await session.commit()
    return Written(data=out, message="Makale yayınlandı.")


@router.patch("/knowledge-articles/{article_id}", summary="Makaleyi güncelle")
async def update_article(
    article_id: uuid.UUID,
    body: KnowledgeArticlePatch,
    current: PlatformAdminDep,
    session: SessionDep,
) -> Written[KnowledgeArticleOut]:
    article = await session.get(KnowledgeArticle, article_id, with_for_update=True)
    if article is None:
        raise NotFoundError("Makale bulunamadı.")
    if body.title is not None:
        article.title = body.title
    if body.body is not None:
        article.body = body.body
    if body.audience is not None:
        article.audience = body.audience.value
    if body.screens is not None:
        article.screens = clean_screens(body.screens)
    article.updated_by_id = current.user.id
    article.updated_by_name = current.user.full_name
    await session.flush()
    await session.refresh(article)
    out = await _article_out(session, article)
    await session.commit()
    return Written(data=out, message="Makale güncellendi.")


@router.get("/knowledge-articles", summary="Bilgi bankası yönetimi (yayında ya da arşivde)")
async def platform_articles(
    _: PlatformAdminDep,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    audience: ArticleAudience | None = None,
    archived: Annotated[bool, Query(description="true: yayından kaldırılanlar")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
) -> Page[KnowledgeArticleOut]:
    return await _article_page(
        session, q=q, screen=None, audience=audience, archived=archived, page=page,
        page_size=page_size,
    )  # fmt: skip


async def _set_archived(
    session: SessionDep, user: User, article_id: uuid.UUID, at: dt.datetime | None
) -> KnowledgeArticleOut:
    article = await session.get(KnowledgeArticle, article_id, with_for_update=True)
    if article is None:
        raise NotFoundError("Makale bulunamadı.")
    if (article.archived_at is None) == (at is None):
        archiving = at is not None
        raise ApiError(
            HTTPStatus.CONFLICT,
            "already_archived" if archiving else "not_archived",
            "Makale zaten yayından kaldırılmış." if archiving else "Makale zaten yayında.",
        )
    article.archived_at = at
    article.updated_by_id = user.id
    article.updated_by_name = user.full_name
    await session.flush()
    await session.refresh(article)
    return await _article_out(session, article)


@router.post(
    "/knowledge-articles/{article_id}/archive", summary="Makaleyi yayından kaldır (silme yok)"
)
async def archive_article(
    article_id: uuid.UUID, current: PlatformAdminDep, session: SessionDep, now: NowDep
) -> Written[KnowledgeArticleOut]:
    out = await _set_archived(session, current.user, article_id, now)
    await session.commit()
    return Written(
        data=out,
        message="Makale yayından kaldırıldı; yardımda ve aramada görünmez. Geri alınabilir.",
    )


@router.post(
    "/knowledge-articles/{article_id}/restore", summary="Arşivdeki makaleyi yeniden yayınla"
)
async def restore_article(
    article_id: uuid.UUID, current: PlatformAdminDep, session: SessionDep
) -> Written[KnowledgeArticleOut]:
    out = await _set_archived(session, current.user, article_id, None)
    await session.commit()
    return Written(data=out, message="Makale yeniden yayınlandı.")
