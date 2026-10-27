"""Ziyaretçi ön daveti ve habersiz ziyaretçide sakin onayı — servis istekleri 41, 42
(Yazılım Dokümanı GUV-02, GUV-03). Modül `visitors` (kapalıysa 404).

- Sakin uçları (`resident/…`): kişiye bağlı üyelik; yalnız bugün bağlı olduğu bölümler (malik,
  kiracı, oturan — vekil hariç). Başka bölümün daveti/talebi 404.
- Görevli uçları: `security.visitors`. Görevli davetin kodunu, sakinin telefonunu görmez.
- Davet kodu yalnız oluşturma ve "yeni kod" yanıtında döner; sunucuda yalnız özeti saklanır.
  Kapıda kodla giriş kullanıcı başına hız sınırlıdır (5 dakikada 30 deneme).
"""

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SettingsDep,
    SiteContext,
    TodayDep,
)
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.gatehouse import (
    access,
    gate_rate_limit,
    my_person,
    my_unit,
    my_unit_ids,
    resident_access,
    rule_error,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.gatehouse import (
    INVITE_CODE_RE,
    DecisionSource,
    InviteStatus,
    InviteType,
    UnannouncedMethod,
    VisitRequestStatus,
    check_invite,
    display_plate,
    seconds_left,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import VisitorKind
from site_yonetim.models import VisitorInvite, VisitRequest
from site_yonetim.services import gatehouse as gate_svc
from site_yonetim.services import security as security_svc
from site_yonetim.services import visits as svc

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])

ResidentVisitors = Annotated[SiteContext, Depends(resident_access(ModuleKey.VISITORS))]
Guard = Annotated[
    SiteContext, Depends(access((ModuleKey.VISITORS,), (Permission.SECURITY_VISITORS,)))
]
CHECK_IN_LIMIT = 30


def _secret(settings: SettingsDep) -> str:
    return settings.jwt_secret.get_secret_value()


Secret = Annotated[str, Depends(_secret)]


# --- Ön davet (sakin) ------------------------------------------------------------------------


class VisitorInviteOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    visitor_name: str
    type: InviteType
    date_from: dt.date
    date_to: dt.date
    weekdays: list[int] = Field(description="0 = pazartesi (yalnız süreli kalıcı)")
    time_from: str | None = Field(description="SS:DD")
    time_to: str | None
    plate: str | None = Field(description="görüntü biçimi: 34 ABC 123")
    note: str | None
    code: str | None = Field(
        description="6 haneli kod — YALNIZ oluşturma ve yeni kod yanıtında; saklanmaz"
    )
    status: InviteStatus
    valid_now: bool
    expired: bool
    created_by: str
    created_at: dt.datetime
    used_at: dt.datetime | None


def _hm(value: dt.time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


async def _invites_out(
    ctx: SiteContext, rows: list[VisitorInvite], now: dt.datetime, code: str | None = None
) -> list[VisitorInviteOut]:
    names = await security_svc.unit_names(ctx.session, {i.unit_id for i in rows})
    at = svc.local(now)
    return [
        VisitorInviteOut(
            id=i.id,
            unit_id=i.unit_id,
            unit_name=names.get(i.unit_id, "—"),
            visitor_name=i.visitor_name,
            type=InviteType(i.type),
            date_from=i.date_from,
            date_to=i.date_to,
            weekdays=list(i.weekdays or []),
            time_from=_hm(i.time_from),
            time_to=_hm(i.time_to),
            plate=display_plate(i.plate) if i.plate else None,
            note=i.note,
            code=code,
            status=InviteStatus(i.status),
            valid_now=i.status == InviteStatus.ACTIVE.value
            and svc.window(i).valid_at(at.date(), at.time()),
            expired=i.status == InviteStatus.ACTIVE.value and i.date_to < at.date(),
            created_by=i.created_by_name,
            created_at=i.created_at,
            used_at=i.used_at,
        )
        for i in rows
    ]


@router.get("/resident/visitor-invites", summary="Ziyaretçi davetlerim")
async def my_invites(ctx: ResidentVisitors, today: TodayDep, now: NowDep) -> list[VisitorInviteOut]:
    units = await my_unit_ids(ctx, today)
    if not units:
        return []
    rows = list(await ctx.session.scalars(svc.resident_invites_query(units, today)))
    return await _invites_out(ctx, rows, now)


class VisitorInviteIn(BaseModel):
    unit_id: uuid.UUID | None = Field(default=None, description="boşsa ilk bölümüm")
    visitor_name: str | None = Field(default=None, max_length=200)
    type: str | None = Field(default=None, description="once · range · recurring")
    date_from: dt.date | None = None
    date_to: dt.date | None = Field(default=None, description="tek kullanımlıkta yok sayılır")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    time_from: dt.time | None = None
    time_to: dt.time | None = None
    plate: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/resident/visitor-invites", status_code=status.HTTP_201_CREATED, summary="Ziyaretçi davet et"
)
async def create_invite(
    ctx: ResidentVisitors,
    body: VisitorInviteIn,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    secret: Secret,
) -> Written[VisitorInviteOut]:
    unit_id = await my_unit(ctx, today, body.unit_id)
    person = my_person(ctx)
    try:
        form = check_invite(
            visitor_name=body.visitor_name,
            type_=body.type,
            date_from=body.date_from,
            date_to=body.date_to,
            weekdays=body.weekdays,
            time_from=body.time_from,
            time_to=body.time_to,
            plate=body.plate,
            note=body.note,
            today=today,
        )
        invite, code = await svc.create_invite(
            ctx.session,
            form,
            unit_id=unit_id,
            inviter=svc.Inviter(person, current.user.id, current.user.full_name),
            settings=(await gate_svc.load_settings(ctx.session)).data,
            site_id=ctx.site.id,
            secret=secret,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _invites_out(ctx, [invite], now, code)
    return Written(
        data=out,
        message=f"Davet oluşturuldu. Kod: {code} — ziyaretçinizle şimdi paylaşın; kapıda kodu ya "
        "da QR'ı gösterir. Kod güvenlik için yeniden gösterilmez (gerekirse yeni kod üretin).",
    )


async def _my_invite(ctx: SiteContext, invite_id: uuid.UUID, today: dt.date) -> VisitorInvite:
    invite = await svc.get_invite(ctx.session, invite_id, await my_unit_ids(ctx, today))
    if invite is None:
        raise NotFoundError("Davet bulunamadı.")
    return invite


@router.post("/resident/visitor-invites/{invite_id}/cancel", summary="Daveti iptal et")
async def cancel_invite(
    invite_id: uuid.UUID, ctx: ResidentVisitors, today: TodayDep, now: NowDep
) -> Written[VisitorInviteOut]:
    invite = await _my_invite(ctx, invite_id, today)
    try:
        await svc.cancel_invite(ctx.session, invite, now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _invites_out(ctx, [invite], now)
    return Written(data=out, message="Davet iptal edildi; kod artık kapıda çalışmaz.")


@router.post("/resident/visitor-invites/{invite_id}/new-code", summary="Yeni davet kodu üret")
async def reissue_code(
    invite_id: uuid.UUID, ctx: ResidentVisitors, today: TodayDep, now: NowDep, secret: Secret
) -> Written[VisitorInviteOut]:
    invite = await _my_invite(ctx, invite_id, today)
    try:
        code = await svc.reissue_code(
            ctx.session, invite, today=today, site_id=ctx.site.id, secret=secret
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _invites_out(ctx, [invite], now, code)
    return Written(data=out, message=f"Yeni kod: {code}. Eski kod artık çalışmaz.")


# --- Kapı: beklenen davetliler ve kodla giriş -----------------------------------------------


class ExpectedInviteOut(BaseModel):
    """Görevlinin gördüğü: ad, bölüm, tip, saat, plaka — kod ve not yok."""

    id: uuid.UUID
    unit_name: str
    visitor_name: str
    type: InviteType
    time_from: str | None
    time_to: str | None
    plate: str | None


@router.get("/visitor-invites/expected", summary="Bugün beklenen davetliler")
async def expected(ctx: Guard, today: TodayDep) -> list[ExpectedInviteOut]:
    rows = await svc.expected_today(ctx.session, today)
    names = await security_svc.unit_names(ctx.session, {i.unit_id for i in rows})
    return [
        ExpectedInviteOut(
            id=i.id,
            unit_name=names.get(i.unit_id, "—"),
            visitor_name=i.visitor_name,
            type=InviteType(i.type),
            time_from=_hm(i.time_from),
            time_to=_hm(i.time_to),
            plate=display_plate(i.plate) if i.plate else None,
        )
        for i in rows
    ]


class InviteCheckInIn(BaseModel):
    code: str | None = Field(default=None, max_length=20)


class InviteCheckInOut(BaseModel):
    visitor_id: uuid.UUID
    invite_id: uuid.UUID
    unit_name: str
    visitor_name: str


@router.post(
    "/visitor-invites/check-in", status_code=status.HTTP_201_CREATED, summary="Kodla içeri al"
)
async def check_in(
    ctx: Guard,
    body: InviteCheckInIn,
    current: CurrentUserDep,
    now: NowDep,
    secret: Secret,
    factory: FactoryDep,
) -> Written[InviteCheckInOut]:
    code = "".join(c for c in (body.code or "") if c.isdigit())
    if not INVITE_CODE_RE.match(code):
        raise rule_error(FormFieldError({"code": "6 haneli davet kodunu girin."}))
    await gate_rate_limit(factory, ctx, current, now, kind="invite", limit=CHECK_IN_LIMIT)
    try:
        result = await svc.check_in(
            ctx.session,
            code,
            now=now,
            site_id=ctx.site.id,
            secret=secret,
            recorded_by=current.user.full_name,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    unit = (await security_svc.unit_names(ctx.session, {result.invite.unit_id})).get(
        result.invite.unit_id, "—"
    )
    return Written(
        data=InviteCheckInOut(
            visitor_id=result.visitor.id,
            invite_id=result.invite.id,
            unit_name=unit,
            visitor_name=result.invite.visitor_name,
        ),
        message=f"{result.invite.visitor_name} ({unit}) içeri alındı; "
        "davet sahibine bilgi verildi.",
    )


# --- Habersiz ziyaretçi --------------------------------------------------------------------


class VisitRequestOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    full_name: str
    kind: VisitorKind
    plate: str | None
    gate_id: uuid.UUID | None
    method: UnannouncedMethod
    status: VisitRequestStatus
    decided_by: str | None
    decision_source: DecisionSource | None
    reason: str | None
    created_by: str
    created_at: dt.datetime
    expires_at: dt.datetime
    timed_out: bool = Field(description="sakin süresi doldu; görevli karar verebilir")
    seconds_left: int
    visitor_id: uuid.UUID | None


async def _requests_out(
    ctx: SiteContext, rows: list[VisitRequest], now: dt.datetime
) -> list[VisitRequestOut]:
    names = await security_svc.unit_names(ctx.session, {r.unit_id for r in rows})
    out = []
    for r in rows:
        awaiting = r.status == VisitRequestStatus.AWAITING.value
        out.append(
            VisitRequestOut(
                id=r.id,
                unit_id=r.unit_id,
                unit_name=names.get(r.unit_id, "—"),
                full_name=r.full_name,
                kind=VisitorKind(r.kind),
                plate=display_plate(r.plate) if r.plate else None,
                gate_id=r.gate_id,
                method=UnannouncedMethod(r.method),
                status=VisitRequestStatus(r.status),
                decided_by=r.decided_by,
                decision_source=DecisionSource(r.decision_source) if r.decision_source else None,
                reason=r.reason,
                created_by=r.created_by,
                created_at=r.created_at,
                expires_at=r.expires_at,
                timed_out=awaiting and now >= r.expires_at,
                seconds_left=seconds_left(r.expires_at, now) if awaiting else 0,
                visitor_id=r.visitor_id,
            )
        )
    return out


class VisitRequestIn(BaseModel):
    unit_id: uuid.UUID
    full_name: str | None = Field(default=None, max_length=200)
    kind: VisitorKind = VisitorKind.GUEST
    plate: str | None = Field(default=None, max_length=20)
    gate_id: uuid.UUID | None = None


@router.post(
    "/visit-requests", status_code=status.HTTP_201_CREATED, summary="Habersiz ziyaretçi kaydı"
)
async def create_request(
    ctx: Guard, body: VisitRequestIn, current: CurrentUserDep, now: NowDep
) -> Written[VisitRequestOut]:
    settings = (await gate_svc.load_settings(ctx.session)).data
    try:
        await gate_svc.require_gate(ctx.session, body.gate_id)
        request = await svc.create_request(
            ctx.session,
            svc.NewVisitRequest(body.unit_id, body.full_name, body.kind, body.plate, body.gate_id),
            settings=settings,
            now=now,
            actor=current.user.full_name,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _requests_out(ctx, [request], now)
    if out.method is UnannouncedMethod.INFO:
        message = f"{out.full_name} içeri alındı; {out.unit_name} sakinlerine bilgi verildi."
    else:
        message = (
            f"{out.unit_name} sakinlerinden onay istendi; "
            f"{settings.approval_timeout_seconds} sn içinde yanıt yoksa siz karar verirsiniz."
        )
    return Written(data=out, message=message)


@router.get("/visit-requests", summary="Bugünkü ve bekleyen habersiz ziyaretçiler")
async def list_requests(
    ctx: Guard,
    today: TodayDep,
    now: NowDep,
    status_filter: Annotated[VisitRequestStatus | None, Query(alias="status")] = None,
) -> list[VisitRequestOut]:
    rows = list(await ctx.session.scalars(svc.requests_query(today, status_filter)))
    return await _requests_out(ctx, rows, now)


class VisitGuardDecisionIn(BaseModel):
    decision: Literal["allow", "deny"] | None = None
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/visit-requests/{request_id}/decide", summary="Görevli kararı (süre dolunca)")
async def guard_decide(
    request_id: uuid.UUID,
    ctx: Guard,
    body: VisitGuardDecisionIn,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[VisitRequestOut]:
    request = await svc.get_request(ctx.session, request_id)
    if request is None:
        raise NotFoundError("Talep bulunamadı.")
    try:
        await svc.guard_decide(
            ctx.session,
            request,
            decision=body.decision,
            reason=body.reason,
            now=now,
            actor=current.user.full_name,
            user_id=current.user.id,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _requests_out(ctx, [request], now)
    allowed = out.status is VisitRequestStatus.ALLOWED
    return Written(
        data=out,
        message=f"{out.full_name} içeri alındı (görevli kararı)."
        if allowed
        else f"{out.full_name} girişi reddedildi; sakine bilgi verildi.",
    )


@router.get("/resident/visit-approvals", summary="Kapıda bekleyen ziyaretçilerim")
async def my_approvals(
    ctx: ResidentVisitors, today: TodayDep, now: NowDep
) -> list[VisitRequestOut]:
    units = await my_unit_ids(ctx, today)
    if not units:
        return []
    rows = list(await ctx.session.scalars(svc.resident_requests_query(units, today)))
    return await _requests_out(ctx, rows, now)


class VisitResidentDecisionIn(BaseModel):
    decision: Literal["allow", "deny"] | None = None


@router.post("/resident/visit-approvals/{request_id}", summary="Ziyaretçiyi onayla / reddet")
async def resident_decide(
    request_id: uuid.UUID,
    ctx: ResidentVisitors,
    body: VisitResidentDecisionIn,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[VisitRequestOut]:
    request = await svc.get_request(ctx.session, request_id, await my_unit_ids(ctx, today))
    if request is None:
        raise NotFoundError("Talep bulunamadı.")
    person = my_person(ctx)
    try:
        await svc.resident_decide(
            ctx.session,
            request,
            decision=body.decision,
            now=now,
            actor=current.user.full_name,
            user_id=current.user.id,
            person_id=person,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _requests_out(ctx, [request], now)
    return Written(
        data=out,
        message="Onayladınız; ziyaretçi içeri alındı."
        if out.status is VisitRequestStatus.ALLOWED
        else "Reddettiniz; görevli bilgilendirildi.",
    )
