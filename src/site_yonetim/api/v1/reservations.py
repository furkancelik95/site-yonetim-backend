"""Rezervasyon uçları — alanlar ve kurallar, rezervasyon (sakin adına, onay, iptal), günlük liste,
kullanım doğrulama, bekleme listesi, kullanım raporu ve sakin uçları (Yazılım Dokümanı §14,
REZ-01..03; servis istekleri 59–61). Modül: `reservations` (kapalıysa 404).

Yetki (servis isteği 59–61):
- Alan listesi, saat durumu, günlük liste, bekleme listesi: `requests.assign` ya da
  `security.visitors`; listeden "geldi" ve kodla giriş de aynı.
- Kural ve K11 ayarı yazma: `modules.manage`.
- Sakin adına rezervasyon, onay, ret, iptal, kullanım raporu: `requests.assign`.
- Sakin uçları (`/resident/…`): kişiye bağlı üyelik; yalnız kendi bölümleri. Hane üyesi yalnız
  "Rezervasyon" yetkisi verildiyse (varsayılan kapalı) rezervasyon yapar.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_module,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import IdempotencyDep, commit_with_key, replayed
from site_yonetim.api.v1.requests import form_error, rule_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ApprovalStatus
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.reservations import (
    ApprovalMode,
    BookingChannel,
    BookingInput,
    ChannelRule,
    DebtRule,
    FeeType,
    ReservationStatus,
    Rules,
    VerifyMethod,
    WaitlistStatus,
    hm,
    parse_hm,
)
from site_yonetim.domain.service_desk import RuleError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import Block, CommonArea, Reservation, ReservationWaitlist, Unit
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services import reservations as svc
from site_yonetim.services.paid_services import billing_account

router = APIRouter(prefix="/sites/{slug}", tags=["rezervasyon"])
resident_router = APIRouter(prefix="/sites/{slug}/resident", tags=["sakin"])
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.RESERVATIONS))]
Paging = Annotated[PageParams, Depends()]
TIME = r"^([01]\d|2[0-3]):[0-5]\d$"
RESIDENT_RECENT = 30


def _can(ctx: SiteContext, *permissions: Permission) -> None:
    if not any(ctx.access.can(p) for p in permissions):
        raise ForbiddenError


def _desk(ctx: SiteContext) -> None:
    """Günlük liste ve giriş: yönetim (talep atama) ya da güvenlik."""
    _can(ctx, Permission.REQUESTS_ASSIGN, Permission.SECURITY_VISITORS)


def _person(current: CurrentUserDep) -> approvals_svc.Person:
    return approvals_svc.Person(current.user.id, current.user.full_name)


# --- Çıktılar --------------------------------------------------------------------------------


class NoShowRuleOut(BaseModel):
    enabled: bool
    months: int
    count: int
    block_days: int


class ReservationRulesOut(BaseModel):
    enabled: bool
    open_from: str = Field(description="SS:DD")
    open_to: str
    slot_minutes: int = Field(description="30/60/90/120/180/240")
    capacity: int = Field(description="aynı saatte kaç rezervasyon")
    max_people: int
    max_guests: int
    weekly_limit: int = Field(description="bölüm başına haftalık sınır; 0 = sınırsız")
    cancel_hours: int = Field(description="başlangıçtan kaç saat öncesine kadar iadeli iptal")
    late_cancel_refund: bool
    fee_type: FeeType
    fee_amount: Money
    channel: ChannelRule
    approval: ApprovalMode
    verify: VerifyMethod
    waitlist: bool
    waitlist_minutes: int = Field(description="bekleme listesi teklif süresi (dakika)")
    noshow: NoShowRuleOut

    @classmethod
    def of(cls, r: Rules) -> ReservationRulesOut:
        return cls(
            enabled=r.enabled,
            open_from=hm(r.open_from),
            open_to=hm(r.open_to),
            slot_minutes=r.slot_minutes,
            capacity=r.capacity,
            max_people=r.max_people,
            max_guests=r.max_guests,
            weekly_limit=r.weekly_limit,
            cancel_hours=r.cancel_hours,
            late_cancel_refund=r.late_cancel_refund,
            fee_type=r.fee_type,
            fee_amount=r.fee_amount,
            channel=r.channel,
            approval=r.approval,
            verify=r.verify,
            waitlist=r.waitlist,
            waitlist_minutes=r.waitlist_minutes,
            noshow=NoShowRuleOut(
                enabled=r.noshow.enabled,
                months=r.noshow.months,
                count=r.noshow.count,
                block_days=r.noshow.block_days,
            ),
        )


class ReservableAreaOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    block_name: str | None
    rules: ReservationRulesOut


async def _areas_out(ctx: SiteContext, rows: list[CommonArea]) -> list[ReservableAreaOut]:
    rules = await svc.rules_for(ctx.session, [a.id for a in rows])
    blocks = await _block_names(ctx, {a.block_id for a in rows if a.block_id})
    return [
        ReservableAreaOut(
            id=a.id,
            name=a.name,
            kind=a.kind,
            block_name=blocks.get(a.block_id) if a.block_id else None,
            rules=ReservationRulesOut.of(rules[a.id]),
        )
        for a in rows
    ]


async def _block_names(ctx: SiteContext, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await ctx.session.execute(select(Block.id, Block.name).where(Block.id.in_(ids)))
    return {row[0]: row[1] for row in rows.all()}


class SlotOut(BaseModel):
    start: str
    end: str
    capacity: int
    taken: int = Field(description="canlı rezervasyon + süresi geçmemiş bekleme teklifi")
    past: bool
    mine: bool = Field(description="sakinin bölümünün bu saatte rezervasyonu var")
    waiting: int = Field(description="bu saat için sırada bekleyen")

    @classmethod
    def of(cls, s: svc.SlotState) -> SlotOut:
        return cls(
            start=hm(s.start),
            end=hm(s.end),
            capacity=s.capacity,
            taken=s.taken,
            past=s.past,
            mine=s.mine,
            waiting=s.waiting,
        )


class ReservationOut(BaseModel):
    id: uuid.UUID
    area_id: uuid.UUID
    area_name: str
    date: dt.date
    start: str
    end: str
    unit_id: uuid.UUID
    unit_name: str
    person_name: str
    people: int
    guests: int
    status: ReservationStatus
    channel: BookingChannel = Field(description="staff: yönetim sakin adına açtı")
    created_by: str
    created_at: dt.datetime
    fee: Money
    charge_id: uuid.UUID | None = Field(description="cari defterdeki ücret kaydı")
    fee_refunded: bool = Field(description="ücret ters kayıtla kapandı")
    approval_request_id: uuid.UUID | None = Field(description="onay kuralı varsa açılan talep")
    approval_status: ApprovalStatus | None
    reject_reason: str | None
    cancel_reason: str | None
    cancelled_late: bool
    checked_in_at: dt.datetime | None
    checked_in_by: str | None
    decided_by: str | None


class ResidentReservationOut(ReservationOut):
    code: str = Field(description="6 haneli giriş kodu; QR içeriği `SY-REZ:{code}`")
    verify: VerifyMethod
    cancel_hours: int


async def _reservations_out(ctx: SiteContext, rows: list[Reservation]) -> list[ReservationOut]:
    units = await requests_svc.unit_names(ctx.session, {r.unit_id for r in rows})
    names = await svc.area_names(ctx.session, {r.area_id for r in rows})
    approvals = await svc.approval_status(
        ctx.session, [r.approval_request_id for r in rows if r.approval_request_id]
    )
    return [
        ReservationOut(
            id=r.id,
            area_id=r.area_id,
            area_name=names.get(r.area_id, "—"),
            date=r.date,
            start=hm(r.start_time),
            end=hm(r.end_time),
            unit_id=r.unit_id,
            unit_name=units.get(r.unit_id, "—"),
            person_name=r.person_name,
            people=r.people,
            guests=r.guests,
            status=ReservationStatus(r.status),
            channel=BookingChannel(r.channel),
            created_by=r.created_by_name,
            created_at=r.created_at,
            fee=r.fee,
            charge_id=r.charge_entry_id,
            fee_refunded=r.refund_entry_id is not None,
            approval_request_id=r.approval_request_id,
            approval_status=approvals.get(r.approval_request_id) if r.approval_request_id else None,
            reject_reason=r.reject_reason,
            cancel_reason=r.cancel_reason,
            cancelled_late=r.cancelled_late,
            checked_in_at=r.checked_in_at,
            checked_in_by=r.checked_in_by_name,
            decided_by=r.decided_by_name,
        )
        for r in rows
    ]


async def _one(ctx: SiteContext, r: Reservation) -> ReservationOut:
    await ctx.session.refresh(r, ["created_at"])
    [data] = await _reservations_out(ctx, [r])
    return data


class WaitlistEntryOut(BaseModel):
    id: uuid.UUID
    area_id: uuid.UUID
    area_name: str
    date: dt.date
    start: str
    end: str
    unit_id: uuid.UUID
    unit_name: str
    person_name: str
    people: int
    guests: int
    status: WaitlistStatus
    offered_until: dt.datetime | None = Field(description="teklif gittiyse son kabul zamanı")
    created_at: dt.datetime


async def _waitlist_out(
    ctx: SiteContext, rows: list[ReservationWaitlist]
) -> list[WaitlistEntryOut]:
    units = await requests_svc.unit_names(ctx.session, {w.unit_id for w in rows})
    names = await svc.area_names(ctx.session, {w.area_id for w in rows})
    return [
        WaitlistEntryOut(
            id=w.id,
            area_id=w.area_id,
            area_name=names.get(w.area_id, "—"),
            date=w.date,
            start=hm(w.start_time),
            end=hm(w.end_time),
            unit_id=w.unit_id,
            unit_name=units.get(w.unit_id, "—"),
            person_name=w.person_name,
            people=w.people,
            guests=w.guests,
            status=WaitlistStatus(w.status),
            offered_until=w.offered_until,
            created_at=w.created_at,
        )
        for w in rows
    ]


class BookingOut(BaseModel):
    status: Literal["approved", "pending", "approval_pending", "waitlisted"]
    reservation: ReservationOut | None
    waitlist: WaitlistEntryOut | None
    position: int | None = Field(description="bekleme listesindeki sıra")
    approval_request_id: uuid.UUID | None


async def _booking_written(
    ctx: SiteContext, result: svc.Booking, response: Response | None
) -> tuple[int, Written[BookingOut]]:
    reservation = await _one(ctx, result.reservation) if result.reservation else None
    waitlist = None
    if result.waitlist is not None:
        await ctx.session.refresh(result.waitlist, ["created_at"])
        [waitlist] = await _waitlist_out(ctx, [result.waitlist])
    if result.waitlist is not None:
        kind: Literal["approved", "pending", "approval_pending", "waitlisted"] = "waitlisted"
    elif result.approval is not None:
        kind = "approval_pending"
    elif reservation is not None and reservation.status is ReservationStatus.APPROVED:
        kind = "approved"
    else:
        kind = "pending"
    code = HTTPStatus.ACCEPTED if kind == "approval_pending" else HTTPStatus.CREATED
    if response is not None:
        response.status_code = code
    return code, Written(
        data=BookingOut(
            status=kind,
            reservation=reservation,
            waitlist=waitlist,
            position=result.position,
            approval_request_id=result.approval.id if result.approval else None,
        ),
        message=" ".join(result.messages),
    )


def _errors(exc: Exception) -> ApiError:
    if isinstance(exc, RuleError):
        return form_error(exc)
    if isinstance(exc, OperationRuleError):
        return rule_error(exc)
    raise exc  # pragma: no cover


# --- 59 Alanlar ve kurallar -------------------------------------------------------------------


@router.get("/reservable-areas", summary="Rezerve edilebilir alanlar ve kuralları")
async def list_areas(ctx: Module) -> list[ReservableAreaOut]:
    _can(ctx, Permission.REQUESTS_ASSIGN, Permission.SECURITY_VISITORS, Permission.MODULES_MANAGE)
    return await _areas_out(ctx, await svc.areas(ctx.session))


class NoShowRuleIn(BaseModel):
    enabled: bool = False
    months: int | None = Field(default=3, ge=0, le=1000)
    count: int | None = Field(default=2, ge=0, le=1000)
    block_days: int | None = Field(default=14, ge=0, le=10000)


class ReservationRulesIn(BaseModel):
    enabled: bool = True
    open_from: str = Field(max_length=5)
    open_to: str = Field(max_length=5)
    slot_minutes: int = Field(ge=0, le=10000)
    capacity: int = Field(ge=0, le=10000)
    max_people: int = Field(ge=0, le=100000)
    max_guests: int = Field(ge=0, le=100000)
    weekly_limit: int = Field(ge=0, le=10000)
    cancel_hours: int = Field(ge=0, le=100000)
    late_cancel_refund: bool = False
    fee_type: FeeType = FeeType.FREE
    fee_amount: Money | None = None
    channel: ChannelRule = ChannelRule.BOTH
    approval: ApprovalMode = ApprovalMode.AUTO
    verify: VerifyMethod = VerifyMethod.CODE
    waitlist: bool = False
    waitlist_minutes: int | None = Field(default=30, ge=0, le=100000)
    noshow: NoShowRuleIn = Field(default_factory=NoShowRuleIn)


@router.put("/reservable-areas/{area_id}/rules", summary="Alan kurallarını kaydet")
async def put_rules(
    area_id: uuid.UUID, ctx: Module, body: ReservationRulesIn
) -> Written[ReservableAreaOut]:
    _can(ctx, Permission.MODULES_MANAGE)
    target = await svc.area(ctx.session, area_id)
    if target is None:
        raise NotFoundError(svc.AREA_MISSING)
    raw = body.model_dump(mode="json")
    try:
        rules = await svc.save_rules(ctx.session, target, raw)
    except RuleError as exc:
        raise form_error(exc) from exc
    [data] = await _areas_out(ctx, [target])
    await ctx.session.commit()
    message = f"{target.name} kuralları kaydedildi."
    if not rules.enabled:
        message += " Alan rezervasyona kapalı."
    return Written(data=data, message=message)


class ReservationSettingsOut(BaseModel):
    debt_block_enabled: bool = Field(
        description="K11: borçlu bölüme rezervasyon kısıtı (site kararı, varsayılan kapalı)"
    )
    debt_min_amount: Money | None = Field(description="bu tutar ve üstü borç")
    debt_min_days: int | None = Field(description="en eski açık borcun gecikme günü (en az)")


class ReservationSettingsIn(BaseModel):
    debt_block_enabled: bool = False
    debt_min_amount: Money | None = None
    debt_min_days: int | None = Field(default=None, ge=0, le=100000)


def _settings_out(rule: DebtRule) -> ReservationSettingsOut:
    return ReservationSettingsOut(
        debt_block_enabled=rule.enabled,
        debt_min_amount=rule.min_amount,
        debt_min_days=rule.min_days,
    )


@router.get("/reservation-settings", summary="Rezervasyon ayarı (K11 borçlu bölüm kısıtı)")
async def get_settings(ctx: Module) -> ReservationSettingsOut:
    _can(ctx, Permission.REQUESTS_ASSIGN, Permission.MODULES_MANAGE)
    return _settings_out(await svc.debt_rule(ctx.session))


@router.put("/reservation-settings", summary="Rezervasyon ayarını kaydet (K11)")
async def put_settings(ctx: Module, body: ReservationSettingsIn) -> Written[ReservationSettingsOut]:
    _can(ctx, Permission.MODULES_MANAGE)
    try:
        rule = await svc.save_debt_rule(
            ctx.session, body.debt_block_enabled, body.debt_min_amount, body.debt_min_days
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    message = (
        f"Borçlu bölüm kısıtı açıldı: {format_money_tr(rule.min_amount or Decimal(0))} ve üstü, "
        f"vadesi {rule.min_days} günü geçen borçta rezervasyon yapılamaz."
        if rule.enabled
        else "Borçlu bölüm kısıtı kapalı; borç rezervasyonu engellemez."
    )
    return Written(data=_settings_out(rule), message=message)


@router.get("/reservable-areas/{area_id}/availability", summary="Günün saat durumu (personel)")
async def staff_availability(
    area_id: uuid.UUID,
    ctx: Module,
    now: NowDep,
    today: TodayDep,
    date: Annotated[dt.date | None, Query(description="boş: bugün")] = None,
) -> list[SlotOut]:
    _desk(ctx)
    target = await svc.area(ctx.session, area_id)
    if target is None:
        raise NotFoundError("Alan bulunamadı.")
    rules = await svc.rules_of(ctx.session, target.id)
    grid = await svc.availability(ctx.session, target.id, rules, date or today, now=now)
    return [SlotOut.of(s) for s in grid]


# --- 60 Rezervasyon (yönetim) -----------------------------------------------------------------


@router.get("/reservations", summary="Rezervasyonlar (günlük liste, onay bekleyen)")
async def list_reservations(
    ctx: Module,
    paging: Paging,
    date: Annotated[dt.date | None, Query()] = None,
    area_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[ReservationStatus | None, Query(alias="status")] = None,
) -> Page[ReservationOut]:
    _desk(ctx)
    query = svc.list_query(day=date, area_id=area_id, status=status_filter)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _reservations_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class StaffReservationIn(BaseModel):
    unit_id: uuid.UUID
    person_name: str | None = Field(default=None, max_length=60, description="boş: hesap sahibi")
    area_id: uuid.UUID
    date: dt.date
    start: str = Field(max_length=5, description="SS:DD")
    people: int = Field(default=1, ge=0, le=100000)
    guests: int = Field(default=0, ge=0, le=100000)


@router.post(
    "/reservations",
    status_code=status.HTTP_201_CREATED,
    summary="Sakin adına rezervasyon (onay kuralı varsa 202)",
    response_model=Written[BookingOut],
)
async def create_for_resident(
    ctx: Module,
    body: StaffReservationIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[BookingOut] | JSONResponse:
    _can(ctx, Permission.REQUESTS_ASSIGN)
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    unit = await ctx.session.get(Unit, body.unit_id)
    if unit is None:
        message = "Bölüm bulunamadı."
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", message,
                       {"unit_id": message})  # fmt: skip
    name = " ".join((body.person_name or "").split())
    if not name:
        account = await billing_account(ctx.session, unit.id)
        if account is not None:
            holder = await requests_svc.person_names(ctx.session, {account.person_id})
            name = holder.get(account.person_id, "")
        name = name or (await requests_svc.unit_names(ctx.session, {unit.id})).get(unit.id, "—")
    booker = svc.Booker(unit.id, None, name, BookingChannel.STAFF, _person(current))
    try:
        result = await svc.create(
            ctx.session, ctx.site, body.area_id,
            BookingInput(body.date, parse_hm(body.start), body.people, body.guests), booker,
            want_waitlist=False, now=now, today=today,
        )  # fmt: skip
    except (RuleError, OperationRuleError) as exc:
        raise _errors(exc) from exc
    code, written = await _booking_written(ctx, result, response)
    await commit_with_key(ctx.session, idem, code, written)
    return written


async def _reservation(ctx: SiteContext, reservation_id: uuid.UUID) -> Reservation:
    r = await svc.get(ctx.session, reservation_id)
    if r is None:
        raise NotFoundError("Rezervasyon bulunamadı.")
    return r


def _offer_text(offers: list[ReservationWaitlist], units: dict[uuid.UUID, str]) -> str:
    if not offers:
        return ""
    names = ", ".join(units.get(o.unit_id, "") for o in offers)
    return f" Bekleme listesindeki {names} bölümüne teklif gitti."


@router.post("/reservations/{reservation_id}/approve", summary="Rezervasyonu onayla")
async def approve_reservation(
    reservation_id: uuid.UUID, ctx: Module, current: CurrentUserDep, now: NowDep, today: TodayDep
) -> Written[ReservationOut]:
    _can(ctx, Permission.REQUESTS_ASSIGN)
    r = await _reservation(ctx, reservation_id)
    try:
        await svc.approve(ctx.session, r, actor=current.user.full_name, now=now, today=today)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = await _one(ctx, r)
    await ctx.session.commit()
    message = "Rezervasyon onaylandı."
    if r.charge_entry_id is not None:
        message += f" Ücret {format_money_tr(r.fee)} borca yansıdı."
    return Written(data=data, message=message)


class ReservationReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/reservations/{reservation_id}/reject", summary="Rezervasyonu reddet (gerekçeli)")
async def reject_reservation(
    reservation_id: uuid.UUID,
    ctx: Module,
    body: ReservationReasonIn,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[ReservationOut]:
    _can(ctx, Permission.REQUESTS_ASSIGN)
    r = await _reservation(ctx, reservation_id)
    try:
        offers = await svc.reject(
            ctx.session, r, body.reason, actor=current.user.full_name, now=now
        )
    except (RuleError, OperationRuleError) as exc:
        raise _errors(exc) from exc
    data = await _one(ctx, r)
    units = await requests_svc.unit_names(ctx.session, {o.unit_id for o in offers})
    await ctx.session.commit()
    return Written(
        data=data,
        message="Rezervasyon reddedildi; ücret oluşmadı." + _offer_text(offers, units),
    )


def _cancel_message(r: Reservation, result: svc.Cancelled, units: dict[uuid.UUID, str]) -> str:
    message = "Rezervasyon iptal edildi."
    if result.refunded:
        message += " Ücret ters kayıtla kapatıldı."
    elif r.charge_entry_id is not None:
        message += " Son iptal süresi geçtiği için ücret iade edilmez."
    return message + _offer_text(result.offers, units)


@router.post("/reservations/{reservation_id}/cancel", summary="Rezervasyonu iptal et (yönetim)")
async def cancel_reservation(
    reservation_id: uuid.UUID,
    ctx: Module,
    body: ReservationReasonIn,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[ReservationOut]:
    _can(ctx, Permission.REQUESTS_ASSIGN)
    r = await _reservation(ctx, reservation_id)
    try:
        result = await svc.cancel(
            ctx.session, r, body.reason, actor=current.user.full_name, by_staff=True, now=now,
            today=today,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = await _one(ctx, r)
    units = await requests_svc.unit_names(ctx.session, {o.unit_id for o in result.offers})
    await ctx.session.commit()
    return Written(data=data, message=_cancel_message(r, result, units) + " Sakine bildirildi.")


@router.post("/reservations/{reservation_id}/check-in", summary="Listeden giriş (geldi)")
async def check_in(
    reservation_id: uuid.UUID, ctx: Module, current: CurrentUserDep, now: NowDep
) -> Written[ReservationOut]:
    _desk(ctx)
    r = await _reservation(ctx, reservation_id)
    try:
        await svc.check_in(ctx.session, r, actor=current.user.full_name, now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = await _one(ctx, r)
    await ctx.session.commit()
    return Written(
        data=data, message=f"{data.unit_name} — {data.area_name} {data.start} girişi kaydedildi."
    )


class CheckInCodeIn(BaseModel):
    code: str = Field(max_length=20, description="6 hane; QR içeriği `SY-REZ:123456` de kabul")


@router.post("/reservations/check-in-code", summary="Kodla doğrula ve giriş kaydet")
async def check_in_code(
    ctx: Module, body: CheckInCodeIn, current: CurrentUserDep, now: NowDep
) -> Written[ReservationOut]:
    _desk(ctx)
    try:
        r = await svc.check_in_code(ctx.session, body.code, actor=current.user.full_name, now=now)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except (RuleError, OperationRuleError) as exc:
        raise _errors(exc) from exc
    data = await _one(ctx, r)
    await ctx.session.commit()
    guests = f" + {data.guests} misafir" if data.guests else ""
    return Written(
        data=data,
        message=f"Doğrulandı: {data.unit_name} · {data.person_name} — {data.area_name} "
        f"{data.start}–{data.end}, {data.people} kişi{guests}.",
    )


@router.get("/waitlist", summary="Bekleme listesi (sırada ve teklif gitmiş)")
async def list_waitlist(
    ctx: Module, paging: Paging, date: Annotated[dt.date | None, Query()] = None
) -> Page[WaitlistEntryOut]:
    _desk(ctx)
    query = svc.waitlist_query(day=date)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _waitlist_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


# --- 61 Kullanım raporu -----------------------------------------------------------------------


class UsageReportRow(BaseModel):
    area_id: uuid.UUID
    area_name: str
    total: int
    cancelled: int
    checked_in: int
    no_show: int
    no_show_rate: int | None = Field(description="doğrulama yöntemi 'yok' olan alanda null")
    occupancy: str = Field(description="doluluk yüzdesi, metin, bir ondalık (`12.5`)")
    revenue: Money = Field(description="borca yansımış, iade edilmemiş ücret")


class UsageReportOut(BaseModel):
    from_: dt.date = Field(alias="from", serialization_alias="from")
    to: dt.date
    rows: list[UsageReportRow]

    model_config = {"populate_by_name": True}


@router.get("/reservations/report", summary="Kullanım raporu (son 30 gün, alan bazında)")
async def usage_report(
    ctx: Module, today: TodayDep, to: Annotated[dt.date | None, Query()] = None
) -> UsageReportOut:
    _can(ctx, Permission.REQUESTS_ASSIGN)
    start, end, rows = await svc.report(ctx.session, to or today)
    return UsageReportOut(
        from_=start,
        to=end,
        rows=[
            UsageReportRow(
                area_id=r.area_id,
                area_name=r.area_name,
                total=r.total,
                cancelled=r.cancelled,
                checked_in=r.checked_in,
                no_show=r.no_show,
                no_show_rate=r.no_show_rate,
                occupancy=f"{r.occupancy:.1f}",
                revenue=r.revenue,
            )
            for r in rows
        ],
    )


# --- Sakin ------------------------------------------------------------------------------------


async def _resident(ctx: SiteContextDep) -> SiteContext:
    if ModuleKey.RESERVATIONS not in ctx.modules:
        raise NotFoundError
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx


Resident = Annotated[SiteContext, Depends(_resident)]


def _person_id(ctx: SiteContext) -> uuid.UUID:
    if ctx.access.person_id is None:  # pragma: no cover — `_resident` engeller
        raise ForbiddenError
    return ctx.access.person_id


async def _my_units(ctx: SiteContext, today: dt.date) -> list[uuid.UUID]:
    return await svc.resident_units(ctx.session, _person_id(ctx), today)


def _resident_visible(rules: Rules) -> bool:
    return rules.enabled and rules.channel is not ChannelRule.STAFF


@resident_router.get("/reservable-areas", summary="Rezervasyona açık alanlar (sakin)")
async def resident_areas(ctx: Resident) -> list[ReservableAreaOut]:
    rows = await _areas_out(ctx, await svc.areas(ctx.session))
    return [a for a in rows if a.rules.enabled and a.rules.channel is not ChannelRule.STAFF]


class ResidentAvailabilityOut(BaseModel):
    slots: list[SlotOut]
    blocked_until: dt.date | None = Field(description="no-show kısıtı bitiş tarihi")
    debt_block: str | None = Field(description="K11 açık ve eşik aşılmışsa gerekçe")


@resident_router.get(
    "/reservable-areas/{area_id}/availability", summary="Günün saat durumu (sakin)"
)
async def resident_availability(
    area_id: uuid.UUID,
    ctx: Resident,
    now: NowDep,
    today: TodayDep,
    date: Annotated[dt.date | None, Query()] = None,
    unit_id: Annotated[uuid.UUID | None, Query(description="boş: ilk bölümü")] = None,
) -> ResidentAvailabilityOut:
    target = await svc.area(ctx.session, area_id)
    rules = await svc.rules_of(ctx.session, area_id) if target else None
    if target is None or rules is None or not _resident_visible(rules):
        raise NotFoundError("Alan bulunamadı.")
    units = await _my_units(ctx, today)
    unit = unit_id if unit_id in units else next(iter(units), None)
    grid = await svc.availability(
        ctx.session, target.id, rules, date or today, now=now, unit_ids=set(units)
    )
    return ResidentAvailabilityOut(
        slots=[SlotOut.of(s) for s in grid],
        blocked_until=(
            await svc.noshow_until(ctx.session, rules, unit, target.id, today) if unit else None
        ),
        debt_block=await svc.debt_block(ctx.session, unit, today) if unit else None,
    )


class ResidentReservationsOut(BaseModel):
    reservations: list[ResidentReservationOut] = Field(description="son 30, yeniden eskiye")
    waitlist: list[WaitlistEntryOut] = Field(description="açık bekleme kayıtları")


@resident_router.get("/reservations", summary="Rezervasyonlarım ve bekleme listem")
async def my_reservations(ctx: Resident, today: TodayDep) -> ResidentReservationsOut:
    units = await _my_units(ctx, today)
    rows = list(
        await ctx.session.scalars(
            select(Reservation)
            .where(Reservation.unit_id.in_(units))
            .order_by(Reservation.starts_at.desc())
            .limit(RESIDENT_RECENT)
        )
    ) if units else []  # fmt: skip
    waits = list(
        await ctx.session.scalars(
            svc.waitlist_query(day=None).where(ReservationWaitlist.unit_id.in_(units))
        )
    ) if units else []  # fmt: skip
    rules = await svc.rules_for(ctx.session, sorted({r.area_id for r in rows}))
    by_id = {r.id: r for r in rows}
    return ResidentReservationsOut(
        reservations=[
            ResidentReservationOut(
                **out.model_dump(),
                code=by_id[out.id].check_in_code,
                verify=rules[out.area_id].verify,
                cancel_hours=rules[out.area_id].cancel_hours,
            )
            for out in await _reservations_out(ctx, rows)
        ],
        waitlist=await _waitlist_out(ctx, waits),
    )


class ResidentReservationIn(BaseModel):
    area_id: uuid.UUID
    date: dt.date
    start: str = Field(max_length=5, description="SS:DD")
    people: int = Field(default=1, ge=0, le=100000)
    guests: int = Field(default=0, ge=0, le=100000)
    waitlist: bool = Field(default=False, description="saat doluysa bekleme listesine gir")
    unit_id: uuid.UUID | None = Field(default=None, description="boş: ilk bölümü")


async def _resident_booker(
    ctx: SiteContext, current: CurrentUserDep, unit_id: uuid.UUID | None, today: dt.date
) -> svc.Booker:
    units = await _my_units(ctx, today)
    unit = unit_id or next(iter(units), None)
    if unit is None or unit not in units:
        message = "Yalnız kendi bölümünüz için rezervasyon yapabilirsiniz."
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "unit_not_yours", message,
                       {"unit_id": message})  # fmt: skip
    if not await svc.may_reserve(ctx.session, _person_id(ctx), unit, today):
        raise ForbiddenError("Bu işlem için yetkiniz yok (hane üyesi rezervasyon yetkisi kapalı).")
    person = _person_id(ctx)
    name = (await requests_svc.person_names(ctx.session, {person})).get(person)
    return svc.Booker(
        unit, person, name or current.user.full_name, BookingChannel.RESIDENT, _person(current)
    )


@resident_router.post(
    "/reservations",
    status_code=status.HTTP_201_CREATED,
    summary="Rezervasyon yap / bekleme listesine gir (onay kuralı varsa 202)",
    response_model=Written[BookingOut],
)
async def resident_create(
    ctx: Resident,
    body: ResidentReservationIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[BookingOut] | JSONResponse:
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    booker = await _resident_booker(ctx, current, body.unit_id, today)
    try:
        result = await svc.create(
            ctx.session, ctx.site, body.area_id,
            BookingInput(body.date, parse_hm(body.start), body.people, body.guests), booker,
            want_waitlist=body.waitlist, now=now, today=today,
        )  # fmt: skip
    except (RuleError, OperationRuleError) as exc:
        raise _errors(exc) from exc
    code, written = await _booking_written(ctx, result, response)
    await commit_with_key(ctx.session, idem, code, written)
    return written


@resident_router.post("/reservations/{reservation_id}/cancel", summary="Rezervasyonumu iptal et")
async def resident_cancel(
    reservation_id: uuid.UUID, ctx: Resident, current: CurrentUserDep, now: NowDep, today: TodayDep
) -> Written[ReservationOut]:
    units = set(await _my_units(ctx, today))
    r = await svc.get(ctx.session, reservation_id, unit_ids=units)
    if r is None:
        raise NotFoundError("Rezervasyon bulunamadı.")
    try:
        result = await svc.cancel(
            ctx.session, r, None, actor=current.user.full_name, by_staff=False, now=now,
            today=today,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = await _one(ctx, r)
    units_names = await requests_svc.unit_names(ctx.session, {o.unit_id for o in result.offers})
    await ctx.session.commit()
    return Written(data=data, message=_cancel_message(r, result, units_names))


@resident_router.post(
    "/waitlist/{entry_id}/accept", status_code=status.HTTP_201_CREATED,
    summary="Bekleme listesi teklifini kabul et",
)  # fmt: skip
async def resident_accept(
    entry_id: uuid.UUID,
    ctx: Resident,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[BookingOut]:
    units = set(await _my_units(ctx, today))
    entry = await svc.get_waitlist(ctx.session, entry_id, unit_ids=units)
    if entry is None:
        raise NotFoundError("Bekleme kaydı bulunamadı.")
    booker = await _resident_booker(ctx, current, entry.unit_id, today)
    try:
        result = await svc.accept_offer(ctx.session, ctx.site, entry, booker, now=now, today=today)
    except (RuleError, OperationRuleError) as exc:
        raise _errors(exc) from exc
    _, written = await _booking_written(ctx, result, response)
    await ctx.session.commit()
    return written


@resident_router.post("/waitlist/{entry_id}/leave", summary="Bekleme listesinden çık")
async def resident_leave(
    entry_id: uuid.UUID, ctx: Resident, now: NowDep, today: TodayDep
) -> Written[WaitlistEntryOut]:
    units = set(await _my_units(ctx, today))
    entry = await svc.get_waitlist(ctx.session, entry_id, unit_ids=units)
    if entry is None:
        raise NotFoundError("Bekleme kaydı bulunamadı.")
    try:
        await svc.leave_waitlist(ctx.session, entry, now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await _waitlist_out(ctx, [entry])
    await ctx.session.commit()
    return Written(data=data, message="Bekleme listesinden çıktınız.")
