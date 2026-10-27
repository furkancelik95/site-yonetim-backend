"""KVKK — Yazılım Dokümanı RAP-04, PLT-01 adım 14–15 (servis isteği 80; PM kararları P6, K8).

- Aydınlatma metni sürümleri (`…/privacy-notices`): okuma site erişimi, yayın `privacy.manage`.
  Yeni sürüm herkesten yeniden onay ister (onay kapısı `api/deps.consent_given`).
- Onay ekranı (`/me/consents`): onay beklerken de çalışır; herkese açık metin
  `/public/privacy-notice/{slug}`.
- İlgili kişi başvurusu: sakin `…/resident/privacy-requests`; yönetim `…/privacy-requests`
  (`privacy.manage`) — 30 gün sayacı, kişisel veri paketi, gerekçeli yanıt.
- Kendi verim (`…/my-privacy`): onay geçmişi, açık rıza ver/geri çek, "verilerimi indir".
- Saklama politikası (`…/settings/retention`, K8) ve VERBİS envanteri (`…/data-inventory`):
  `privacy.manage`.
"""

import datetime as dt
import json
import uuid
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    FactoryDep,
    NowDep,
    PasswordChangedUserDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    client_ip,
    require_permission,
    resolved_access,
    user_agent,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.resident import resident
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission, UserAccess
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.privacy import (
    KIND_LABELS,
    OUTCOME_LABELS,
    PURPOSE_LABELS,
    ExplicitPurpose,
    PrivacyFieldError,
    PrivacyOutcome,
    PrivacyRequestKind,
    PrivacyRequestStatus,
    check_answer,
    check_notice,
    check_request,
    days_left,
)
from site_yonetim.domain.retention import (
    ACTION_LABELS,
    BY_KEY,
    UNIT_LABELS,
    RetentionAction,
    RetentionFieldError,
    RetentionUnit,
    RunTrigger,
    check_value,
)
from site_yonetim.models import PrivacyNotice, PrivacyRequest, RetentionRun
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import data_inventory as inventory_svc
from site_yonetim.services import exports, rate_limit
from site_yonetim.services import privacy as svc
from site_yonetim.services import retention as retention_svc

router = APIRouter(prefix="/sites/{slug}", tags=["kvkk"])
me_router = APIRouter(prefix="/me", tags=["kvkk"])
public_router = APIRouter(prefix="/public", tags=["kvkk"])

Paging = Annotated[PageParams, Depends()]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.PRIVACY_MANAGE))]
Resident = Annotated[SiteContext, Depends(resident)]
PUBLIC_LIMIT = 60
PUBLIC_WINDOW = dt.timedelta(minutes=10)
NOT_FOUND = "Başvuru bulunamadı."


def _form_error(fields: dict[str, str]) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, fields)


def _rule_error(exc: svc.PrivacyRuleError) -> ApiError:
    return ApiError(HTTPStatus(exc.status), exc.code, exc.message)


async def _page[T](ctx: SiteContext, query: Select[T], paging: PageParams) -> tuple[list[T], int]:
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return rows, total


# --- Aydınlatma metni -------------------------------------------------------------------------


class PrivacyNoticeOut(BaseModel):
    id: uuid.UUID
    version: int
    text: str
    summary: str
    published_at: dt.datetime
    published_by: str

    @classmethod
    def of(cls, n: PrivacyNotice) -> PrivacyNoticeOut:
        return cls(
            id=n.id,
            version=n.version,
            text=n.text,
            summary=n.summary,
            published_at=n.created_at,
            published_by=n.published_by_name,
        )


class PrivacyNoticeIn(BaseModel):
    text: str | None = Field(default=None, max_length=60_000)
    summary: str | None = Field(default=None, max_length=400)


@router.get("/privacy-notices", summary="Aydınlatma metni sürümleri (yeni üstte; sayfalı)")
async def list_notices(ctx: SiteContextDep, paging: Paging) -> Page[PrivacyNoticeOut]:
    rows, total = await _page(ctx, svc.notices_query(), paging)
    return Page(
        items=[PrivacyNoticeOut.of(n) for n in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post(
    "/privacy-notices",
    status_code=status.HTTP_201_CREATED,
    summary="Aydınlatma metninin yeni sürümünü yayınla (herkes yeniden onaylar)",
)
async def publish_notice(
    ctx: Manager, body: PrivacyNoticeIn, current: CurrentUserDep, request: Request
) -> Written[PrivacyNoticeOut]:
    try:
        checked = check_notice(body.text, body.summary)
    except PrivacyFieldError as exc:
        raise _form_error(exc.fields) from None
    notice = await svc.publish(
        ctx.session,
        ctx.site,
        checked,
        user_id=current.user.id,
        actor=current.user.full_name,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    out = PrivacyNoticeOut.of(notice)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"Aydınlatma metni sürüm {notice.version} yayınlandı; eski sürümler saklanır. "
        "Kullanıcılar bir sonraki işlemlerinde yeni metni okuyup onaylayacak.",
    )


class PublicNoticeOut(BaseModel):
    site_name: str
    version: int | None = Field(description="yayınlanmış sürüm yoksa null (taslak gösterilir)")
    text: str | None
    published_at: dt.datetime | None


@public_router.get("/privacy-notice/{slug}", summary="Sitenin güncel aydınlatma metni (oturumsuz)")
async def public_notice(
    slug: str, request: Request, factory: FactoryDep, now: NowDep, response: Response
) -> PublicNoticeOut:
    ip = request.client.host if request.client else "unknown"
    if not await rate_limit.hit(
        factory, key=f"privacy-notice:{ip}", now=now, limit=PUBLIC_LIMIT, window=PUBLIC_WINDOW
    ):
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_requests",
            "Çok fazla istek yaptınız, biraz sonra tekrar deneyin.",
        )
    found = await svc.public_notice(factory, slug) if len(slug) <= 80 else None
    if found is None:
        raise NotFoundError("Site bulunamadı.")
    site, notice = found
    response.headers["Cache-Control"] = "no-store"
    return PublicNoticeOut(
        site_name=site.name,
        version=notice.version if notice else None,
        text=notice.text if notice else None,
        published_at=notice.created_at if notice else None,
    )


# --- Onay ekranı (P6) -------------------------------------------------------------------------


class PendingNoticeOut(BaseModel):
    notice_id: uuid.UUID
    site_id: uuid.UUID
    site_name: str
    site_slug: str
    version: int
    text: str
    summary: str
    published_at: dt.datetime


class GivenConsentOut(BaseModel):
    site_id: uuid.UUID
    site_name: str
    version: int
    source: str
    accepted_at: dt.datetime


class ConsentStateOut(BaseModel):
    consent_required: bool
    pending: list[PendingNoticeOut]
    given: list[GivenConsentOut] = Field(description="onay geçmişi, yeni üstte")


ResolvedAccess = Annotated[UserAccess, Depends(resolved_access)]


async def _state(factory: FactoryDep, user_id: uuid.UUID, access: UserAccess) -> ConsentStateOut:
    sites = {s.site_id: s for s in access.sites}
    ids = list(sites)
    pending = await svc.pending_notices(factory, user_id, ids)
    given = await svc.given_consents(factory, user_id, ids)
    return ConsentStateOut(
        consent_required=bool(pending),
        pending=[
            PendingNoticeOut(
                notice_id=p.notice_id,
                site_id=p.site_id,
                site_name=sites[p.site_id].name,
                site_slug=sites[p.site_id].slug,
                version=p.version,
                text=p.text,
                summary=p.summary,
                published_at=p.published_at,
            )
            for p in pending
        ],
        given=[
            GivenConsentOut(
                site_id=g.site_id,
                site_name=sites[g.site_id].name,
                version=g.version,
                source=g.source,
                accepted_at=g.accepted_at,
            )
            for g in given
        ],
    )


@me_router.get("/consents", summary="Onay bekleyen aydınlatma metinleri ve onay geçmişim")
async def my_consents(
    current: PasswordChangedUserDep, access: ResolvedAccess, factory: FactoryDep
) -> ConsentStateOut:
    """Onay beklerken de çalışır (girişten sonraki onay ekranı)."""
    return await _state(factory, current.user.id, access)


class ConsentIn(BaseModel):
    notice_ids: list[uuid.UUID] = Field(max_length=50, description="onaylanan sürümler")


@me_router.post("/consents", summary="Aydınlatma metnini onayla")
async def accept_consents(
    body: ConsentIn,
    current: PasswordChangedUserDep,
    access: ResolvedAccess,
    factory: FactoryDep,
    request: Request,
) -> Written[ConsentStateOut]:
    try:
        count = await svc.accept(
            factory,
            current.user.id,
            [s.site_id for s in access.sites],
            body.notice_ids,
            ip=client_ip(request),
            user_agent=user_agent(request),
        )
    except svc.PrivacyRuleError as exc:
        raise _rule_error(exc) from None
    state = await _state(factory, current.user.id, access)
    return Written(
        data=state,
        message="Aydınlatma metni onaylandı." if count == 1 else f"{count} metin onaylandı.",
    )


# --- Kendi verim: onaylar, açık rıza, verilerimi indir ---------------------------------------


class ExplicitConsentOut(BaseModel):
    purpose: ExplicitPurpose
    label: str
    granted: bool
    changed_at: dt.datetime | None


class MyPrivacyOut(BaseModel):
    notice: PrivacyNoticeOut | None = Field(description="sitenin güncel aydınlatma metni")
    accepted: list[GivenConsentOut]
    explicit: list[ExplicitConsentOut]


async def _my_privacy(ctx: SiteContext, user_id: uuid.UUID, factory: FactoryDep) -> MyPrivacyOut:
    notice = await svc.latest_notice(ctx.session)
    state = await svc.explicit_state(ctx.session, user_id)
    given = await svc.given_consents(factory, user_id, [ctx.site.id])
    return MyPrivacyOut(
        notice=PrivacyNoticeOut.of(notice) if notice else None,
        accepted=[
            GivenConsentOut(
                site_id=g.site_id,
                site_name=ctx.site.name,
                version=g.version,
                source=g.source,
                accepted_at=g.accepted_at,
            )
            for g in given
        ],
        explicit=[
            ExplicitConsentOut(
                purpose=p,
                label=PURPOSE_LABELS[p],
                granted=bool(state.get(p) and state[p].granted),
                changed_at=state[p].at if p in state else None,
            )
            for p in ExplicitPurpose
        ],
    )


@router.get("/my-privacy", summary="Bu sitedeki onaylarım ve açık rızam")
async def my_privacy(
    ctx: SiteContextDep, current: CurrentUserDep, factory: FactoryDep
) -> MyPrivacyOut:
    return await _my_privacy(ctx, current.user.id, factory)


class ExplicitIn(BaseModel):
    granted: bool


@router.put(
    "/my-privacy/explicit-consents/{purpose}",
    summary="Açık rıza ver / geri çek (isteğe bağlı; hizmete şart değil)",
)
async def set_explicit(
    ctx: SiteContextDep,
    purpose: ExplicitPurpose,
    body: ExplicitIn,
    current: CurrentUserDep,
    factory: FactoryDep,
    request: Request,
) -> Written[MyPrivacyOut]:
    await svc.set_explicit(
        ctx.session, current.user.id, purpose, granted=body.granted, ip=client_ip(request)
    )
    await ctx.session.commit()
    out = await _my_privacy(ctx, current.user.id, factory)
    return Written(
        data=out,
        message="Açık rızanız kaydedildi."
        if body.granted
        else "Açık rızanızı geri çektiniz; hizmetler aynen devam eder.",
    )


@router.get(
    "/my-privacy/data-export",
    summary="Verilerimi indir (kişisel veri paketi, JSON)",
    response_model=dict[str, Any],
)
async def my_data_export(ctx: SiteContextDep, current: CurrentUserDep, now: NowDep) -> Response:
    package = await svc.data_package(
        ctx.session, ctx.site, user_id=current.user.id, person_id=ctx.access.person_id, now=now
    )
    record(ctx.session, AuditAction.EXPORT, "privacy_requests", None, {"self_export": True})
    await ctx.session.commit()
    return _json_download(package, f"kisisel-verilerim-{ctx.site.slug}.json")


def _json_download(data: dict[str, Any], name: str) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


# --- İlgili kişi başvurusu ------------------------------------------------------------------


class PrivacyRequestIn(BaseModel):
    kind: str | None = Field(
        default=None, max_length=20, description="access · rectify · erase · object"
    )
    detail: str | None = Field(default=None, max_length=4000)


class MyPrivacyRequestOut(BaseModel):
    id: uuid.UUID
    kind: PrivacyRequestKind
    kind_label: str
    detail: str
    status: PrivacyRequestStatus
    created_at: dt.datetime
    due_on: dt.date
    response: str | None
    outcome: PrivacyOutcome | None
    outcome_label: str | None
    retained: str | None
    answered_at: dt.datetime | None


class PrivacyRequestOut(MyPrivacyRequestOut):
    name: str
    units: list[str]
    days_left: int = Field(description="eksi: gecikme günü")
    answered_by: str | None


def _mine(r: PrivacyRequest) -> dict[str, Any]:
    outcome = PrivacyOutcome(r.outcome) if r.outcome else None
    return {
        "id": r.id,
        "kind": PrivacyRequestKind(r.kind),
        "kind_label": KIND_LABELS[PrivacyRequestKind(r.kind)],
        "detail": r.detail,
        "status": PrivacyRequestStatus(r.status),
        "created_at": r.created_at,
        "due_on": r.due_on,
        "response": r.response,
        "outcome": outcome,
        "outcome_label": OUTCOME_LABELS[outcome] if outcome else None,
        "retained": r.retained,
        "answered_at": r.answered_at,
    }


def _full(r: PrivacyRequest, today: dt.date) -> PrivacyRequestOut:
    return PrivacyRequestOut(
        **_mine(r),
        name=r.requester_name,
        units=list(r.unit_names),
        days_left=days_left(r.due_on, today),
        answered_by=r.answered_by_name,
    )


@router.post(
    "/resident/privacy-requests",
    status_code=status.HTTP_201_CREATED,
    summary="KVKK başvurusu yap (sakin) — 30 gün sayacı başlar",
)
async def create_privacy_request(
    ctx: Resident, body: PrivacyRequestIn, current: CurrentUserDep, today: TodayDep
) -> Written[MyPrivacyRequestOut]:
    try:
        checked = check_request(body.kind, body.detail)
    except PrivacyFieldError as exc:
        raise _form_error(exc.fields) from None
    assert ctx.access.person_id is not None  # noqa: S101  # nosec B101 — `resident` garanti eder
    try:
        created = await svc.create_request(
            ctx.session,
            ctx.site,
            checked,
            user=current.user,
            person_id=ctx.access.person_id,
            today=today,
        )
    except svc.PrivacyRuleError as exc:
        raise _rule_error(exc) from None
    out = MyPrivacyRequestOut(**_mine(created))
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"Başvurunuz alındı; en geç {created.due_on:%d.%m.%Y} tarihine kadar "
        "yanıtlanır (30 gün).",
    )


@router.get("/resident/privacy-requests", summary="KVKK başvurularım (sayfalı)")
async def my_privacy_requests(
    ctx: Resident, current: CurrentUserDep, paging: Paging
) -> Page[MyPrivacyRequestOut]:
    query = svc.requests_query(current.user.id)
    rows, total = await _page(ctx, query, paging)
    return Page(
        items=[MyPrivacyRequestOut(**_mine(r)) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get("/privacy-requests", summary="KVKK başvuruları (açıklar önce, son güne göre; sayfalı)")
async def list_privacy_requests(
    ctx: Manager,
    paging: Paging,
    today: TodayDep,
    status_: Annotated[
        PrivacyRequestStatus | None, Query(alias="status", description="open · answered")
    ] = None,
) -> Page[PrivacyRequestOut]:
    query = svc.requests_query()
    if status_ is not None:
        query = query.where(PrivacyRequest.status == status_.value)
    rows, total = await _page(ctx, query, paging)
    return Page(
        items=[_full(r, today) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _request(
    ctx: SiteContext, request_id: uuid.UUID, *, lock: bool = False
) -> PrivacyRequest:
    found = await svc.get_request(ctx.session, request_id, lock=lock)
    if found is None:
        raise NotFoundError(NOT_FOUND)
    return found


@router.get(
    "/privacy-requests/{request_id}/data-package",
    summary="Kişisel veri paketi (JSON) — erişim başvurusu",
    response_model=dict[str, Any],
)
async def data_package(ctx: Manager, request_id: uuid.UUID, now: NowDep) -> Response:
    found = await _request(ctx, request_id)
    package = await svc.data_package(
        ctx.session, ctx.site, user_id=found.user_id, person_id=found.person_id, now=now
    )
    record(ctx.session, AuditAction.EXPORT, "privacy_requests", found.id, {"data_package": True})
    await ctx.session.commit()
    return _json_download(package, f"kisisel-veri-{found.id}.json")


class PrivacyAnswerIn(BaseModel):
    response: str | None = Field(default=None, max_length=8000)
    outcome: str | None = Field(default=None, max_length=20)
    retained: str | None = Field(default=None, max_length=4000)


@router.post("/privacy-requests/{request_id}/answer", summary="Başvuruyu gerekçeli yanıtla")
async def answer_privacy_request(
    ctx: Manager,
    request_id: uuid.UUID,
    body: PrivacyAnswerIn,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[PrivacyRequestOut]:
    found = await _request(ctx, request_id, lock=True)
    if found.status != PrivacyRequestStatus.OPEN.value:
        raise ApiError(HTTPStatus.CONFLICT, "invalid_status", "Başvuru zaten yanıtlandı.")
    try:
        checked = check_answer(
            PrivacyRequestKind(found.kind), body.response, body.outcome, body.retained
        )
    except PrivacyFieldError as exc:
        raise _form_error(exc.fields) from None
    await svc.answer(ctx.session, found, checked, actor=current.user.full_name, now=now)
    out = _full(found, today)
    await ctx.session.commit()
    return Written(data=out, message="Başvuru yanıtlandı; kişiye bildirim gitti.")


# --- Saklama politikası (K8) -----------------------------------------------------------------


class RetentionRunOut(BaseModel):
    id: uuid.UUID
    category: str
    category_label: str
    action: RetentionAction
    trigger: RunTrigger
    cutoff: dt.date
    affected: int
    actor: str
    at: dt.datetime

    @classmethod
    def of(cls, r: RetentionRun) -> RetentionRunOut:
        category = BY_KEY.get(r.category)
        return cls(
            id=r.id,
            category=r.category,
            category_label=category.label if category else r.category,
            action=RetentionAction(r.action),
            trigger=RunTrigger(r.trigger),
            cutoff=r.cutoff,
            affected=r.affected,
            actor=r.actor_name,
            at=r.created_at,
        )


class RetentionCategoryOut(BaseModel):
    key: str
    label: str
    data: str
    kvkk_category: str
    subjects: str
    value: int
    unit: RetentionUnit
    unit_label: str
    default_value: int
    max_value: int | None
    action: RetentionAction
    action_label: str
    adjustable: bool = Field(description="false: yasal saklama, otomatik işlem yok")
    auto_apply: bool = Field(description="gece işi uygular (hukuki teyide kadar varsayılan kapalı)")
    needs_legal_review: bool
    legal_basis: str
    cutoff: dt.date | None = Field(description="bu tarihten önceki kayıtların süresi doldu")
    pending: int | None = Field(description="süresi dolmuş, henüz işlenmemiş kayıt sayısı")
    last_run: RetentionRunOut | None


class RetentionOut(BaseModel):
    categories: list[RetentionCategoryOut]


async def _retention(ctx: SiteContext, today: dt.date) -> RetentionOut:
    rows = await retention_svc.policy(ctx.session, today)
    return RetentionOut(
        categories=[
            RetentionCategoryOut(
                key=r.category.key,
                label=r.category.label,
                data=r.category.data,
                kvkk_category=r.category.kvkk_category,
                subjects=r.category.subjects,
                value=r.value,
                unit=r.category.unit,
                unit_label=UNIT_LABELS[r.category.unit],
                default_value=r.category.default_value,
                max_value=r.category.max_value,
                action=r.category.action,
                action_label=ACTION_LABELS[r.category.action],
                adjustable=r.category.adjustable,
                auto_apply=r.auto_apply,
                needs_legal_review=r.category.needs_legal_review,
                legal_basis=r.category.legal_basis,
                cutoff=r.cutoff,
                pending=r.pending,
                last_run=RetentionRunOut.of(r.last_run) if r.last_run else None,
            )
            for r in rows
        ]
    )


@router.get("/settings/retention", summary="KVKK saklama politikası (kategori × süre)")
async def retention_policy(ctx: Manager, today: TodayDep) -> RetentionOut:
    return await _retention(ctx, today)


class RetentionItemIn(BaseModel):
    key: str = Field(max_length=40)
    value: int
    auto_apply: bool = False


class RetentionIn(BaseModel):
    items: list[RetentionItemIn] = Field(max_length=20)


@router.put("/settings/retention", summary="Saklama sürelerini ve otomatik uygulamayı kaydet")
async def save_retention(
    ctx: Manager, body: RetentionIn, current: CurrentUserDep, today: TodayDep
) -> Written[RetentionOut]:
    errors: dict[str, str] = {}
    checked: list[tuple[str, retention_svc.PolicyValue]] = []
    for item in body.items:
        category = BY_KEY.get(item.key)
        if category is None or retention_svc.store_of(item.key) is None:
            errors[item.key] = "Bu kategori ayarlanamaz (yasal saklama ya da tanımsız)."
            continue
        try:
            amount = check_value(category, item.value)
        except RetentionFieldError as exc:
            errors.update(exc.fields)
            continue
        checked.append((item.key, retention_svc.PolicyValue(amount, item.auto_apply)))
    if errors:
        raise _form_error(errors)
    for key, value in checked:
        await retention_svc.save(ctx.session, key, value, actor=current.user.full_name)
    out = await _retention(ctx, today)
    await ctx.session.commit()
    return Written(data=out, message="Saklama politikası kaydedildi.")


@router.post(
    "/settings/retention/{key}/apply",
    summary="Süresi dolan kayıtları şimdi işle (silme/anonimleştirme raporu yazılır)",
)
async def apply_retention(
    ctx: Manager, key: str, current: CurrentUserDep, today: TodayDep, now: NowDep
) -> Written[RetentionRunOut]:
    if key not in BY_KEY or retention_svc.store_of(key) is None:
        raise NotFoundError("Saklama kategorisi bulunamadı.")
    run = await retention_svc.apply(
        ctx.session,
        key,
        today=today,
        now=now,
        trigger=RunTrigger.MANUAL,
        actor=current.user.full_name,
    )
    await ctx.session.flush()
    out = RetentionRunOut.of(run)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"{run.affected} kayıt işlendi; silme raporuna yazıldı."
        if run.affected
        else "Süresi dolan kayıt yok; rapora yazıldı.",
    )


@router.get("/settings/retention/runs", summary="Silme/anonimleştirme raporu (yeni üstte; sayfalı)")
async def retention_runs(ctx: Manager, paging: Paging) -> Page[RetentionRunOut]:
    rows, total = await _page(ctx, retention_svc.runs_query(), paging)
    return Page(
        items=[RetentionRunOut.of(r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


# --- VERBİS envanteri ------------------------------------------------------------------------


class InventoryRowOut(BaseModel):
    category: str
    category_label: str
    data: str
    subjects: str
    purpose: str
    legal_basis: str
    retention: str
    recipients: str
    source: str
    records: int


@router.get("/data-inventory", summary="Kişisel veri envanteri (VERBİS dökümü)")
async def data_inventory(ctx: Manager, today: TodayDep) -> list[InventoryRowOut]:
    rows = await inventory_svc.inventory(ctx.session, today)
    return [_row(r) for r in rows]


def _row(r: inventory_svc.InventoryRow) -> InventoryRowOut:
    return InventoryRowOut(
        category=r.category,
        category_label=r.category_label,
        data=r.data,
        subjects=r.subjects,
        purpose=r.purpose,
        legal_basis=r.legal_basis,
        retention=r.retention,
        recipients=r.recipients,
        source=r.source,
        records=r.records,
    )


@router.get(
    "/data-inventory/export",
    summary="Kişisel veri envanteri dışa aktarma (Excel ya da CSV)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX_MEDIA_TYPE: {}, exports.CSV_MEDIA_TYPE: {}}}},
)
async def data_inventory_export(
    ctx: Manager,
    today: TodayDep,
    format_: Annotated[Literal["xlsx", "csv"], Query(alias="format")] = "xlsx",
) -> Response:
    rows = inventory_svc.as_rows(await inventory_svc.inventory(ctx.session, today))
    record(ctx.session, AuditAction.EXPORT, "data_inventory", None, {"format": format_})
    await ctx.session.commit()
    name = f"veri-envanteri-{ctx.site.slug}-{today:%Y%m%d}"
    if format_ == "csv":
        return Response(
            exports.rows_csv(inventory_svc.HEADER, rows),
            media_type=exports.CSV_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    return Response(
        exports.rows_xlsx("Envanter", inventory_svc.HEADER, rows),
        media_type=exports.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
    )
