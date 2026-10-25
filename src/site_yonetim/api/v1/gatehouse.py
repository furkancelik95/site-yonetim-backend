"""Kapılar, güvenlik ayarları, kargo teslim yetkilisi, uzun bekleyen kargo — servis istekleri 40, 44
(Yazılım Dokümanı GUV-05, GUV-08). Ortak kapı yardımcıları (erişim, hata, hız sınırı) burada.

- Kapı ve ayar yazma: `modules.manage`; okuma güvenlik rolünde de (kapı listesi ve yöntem).
  Modül `visitors` ya da `packages` (ikisi de kapalıysa 404).
- Saklama süresi üst sınırı 24 ay (422 `retention_limit`); varsayılan 6 ay (K8). Gece
  anonimleştirme işi varsayılan **kapalı** (`auto_anonymize`).
- Teslim yetkilisi: sakin kendi bölümüne en fazla 3 kişi (409 `delegate_limit`); görevli yalnız
  ad görür.
"""

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    CurrentUser,
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
)
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.gatehouse import (
    GateKind,
    ParcelMethod,
    UnannouncedMethod,
    VisitorFields,
    check_settings,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Gate, ParcelDelegate
from site_yonetim.services import gatehouse as svc
from site_yonetim.services import rate_limit
from site_yonetim.services import security as security_svc

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])

GATE_WINDOW = timedelta(minutes=5)


# --- Ortak yardımcılar ------------------------------------------------------------------------


def access(
    modules: tuple[ModuleKey, ...], permissions: tuple[Permission, ...]
) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """Modüllerden biri açık değilse 404, izinlerden biri yoksa 403."""

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if not any(m in ctx.modules for m in modules):
            raise NotFoundError
        if not any(ctx.access.can(p) for p in permissions):
            raise ForbiddenError
        return ctx

    return dependency


def resident_access(module: ModuleKey) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """Sakin uçları: kişiye bağlı üyelik (yoksa 403) ve modül (kapalıysa 404)."""

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if module not in ctx.modules:
            raise NotFoundError
        if ctx.access.person_id is None:
            raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
        return ctx

    return dependency


def rule_error(exc: OperationRuleError | FormFieldError) -> ApiError:
    if isinstance(exc, FormFieldError):
        return form_error(exc)
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    status_code = (
        HTTPStatus.NOT_FOUND
        if exc.code.endswith("not_found") and not exc.field
        else (HTTPStatus.UNPROCESSABLE_ENTITY)
    )
    return ApiError(status_code, exc.code, exc.message, fields)


async def gate_rate_limit(
    factory: FactoryDep,
    ctx: SiteContext,
    current: CurrentUser,
    now: dt.datetime,
    *,
    kind: str,
    limit: int,
) -> None:
    """Kapı uçlarında kullanıcı başına hız sınırı (kod tahminine karşı)."""
    allowed = await rate_limit.hit(
        factory,
        key=f"gate-{kind}:{ctx.site.id}:{current.user.id}",
        now=now,
        limit=limit,
        window=GATE_WINDOW,
    )
    if not allowed:
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_requests",
            "Çok fazla deneme yaptınız; birkaç dakika sonra tekrar deneyin.",
            headers={"Retry-After": str(int(GATE_WINDOW.total_seconds()))},
        )


def my_person(ctx: SiteContext) -> uuid.UUID:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - resident_access engeller
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return person


async def my_unit_ids(ctx: SiteContext, today: dt.date) -> set[uuid.UUID]:
    return await security_svc.person_unit_ids(ctx.session, my_person(ctx), today)


async def my_unit(ctx: SiteContext, today: dt.date, unit_id: uuid.UUID | None) -> uuid.UUID:
    """Sakinin seçtiği (yoksa ilk) bölümü; başkasının bölümü 404."""
    units = await my_unit_ids(ctx, today)
    if unit_id is not None:
        if unit_id not in units:
            raise NotFoundError("Bölümünüz bulunamadı.")
        return unit_id
    if not units:
        raise NotFoundError("Bölümünüz bulunamadı.")
    names = await security_svc.unit_names(ctx.session, units)
    return min(units, key=lambda u: names.get(u, ""))


SETUP = (ModuleKey.VISITORS, ModuleKey.PACKAGES)
GUARD_PERMS = (
    Permission.SECURITY_VISITORS,
    Permission.SECURITY_PACKAGES,
    Permission.SECURITY_INCIDENTS,
)
SetupRead = Annotated[
    SiteContext, Depends(access(SETUP, (Permission.MODULES_MANAGE, *GUARD_PERMS)))
]
SetupWrite = Annotated[SiteContext, Depends(access(SETUP, (Permission.MODULES_MANAGE,)))]


# --- Kapılar -------------------------------------------------------------------------------


class GateOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: GateKind
    block_ids: list[uuid.UUID]
    always_open: bool
    is_active: bool


def _gate(g: Gate) -> GateOut:
    return GateOut(
        id=g.id,
        name=g.name,
        kind=GateKind(g.kind),
        block_ids=list(g.block_ids or []),
        always_open=g.always_open,
        is_active=g.is_active,
    )


class GateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    kind: str | None = Field(default=None, description="vehicle · pedestrian · service")
    block_ids: list[uuid.UUID] = Field(default_factory=list, description="boş: tüm bloklar")
    always_open: bool = True
    is_active: bool = True


@router.get("/gates", summary="Kapılar")
async def list_gates(ctx: SetupRead) -> list[GateOut]:
    return [_gate(g) for g in await svc.list_gates(ctx.session)]


async def _save_gate(ctx: SiteContext, gate: Gate | None, body: GateIn) -> GateOut:
    try:
        saved = await svc.save_gate(
            ctx.session,
            gate,
            svc.GateInput(body.name, body.kind, body.block_ids, body.always_open, body.is_active),
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return _gate(saved)


@router.post("/gates", status_code=status.HTTP_201_CREATED, summary="Kapı ekle")
async def create_gate(ctx: SetupWrite, body: GateIn) -> Written[GateOut]:
    gate = await _save_gate(ctx, None, body)
    return Written(data=gate, message=f"{gate.name} eklendi.")


@router.patch("/gates/{gate_id}", summary="Kapıyı düzenle")
async def update_gate(gate_id: uuid.UUID, ctx: SetupWrite, body: GateIn) -> Written[GateOut]:
    gate = await svc.get_gate(ctx.session, gate_id)
    if gate is None:
        raise NotFoundError("Kapı bulunamadı.")
    out = await _save_gate(ctx, gate, body)
    return Written(data=out, message=f"{out.name} güncellendi.")


# --- Güvenlik ayarları ----------------------------------------------------------------------


class VisitorFieldsSchema(BaseModel):
    plate: bool = True
    phone: bool = False
    id_seen: bool = Field(default=False, description="yalnız işaret; kimlik no tutulmaz")


class SecuritySettingsOut(BaseModel):
    unannounced_method: UnannouncedMethod
    approval_timeout_seconds: int
    gate_methods: dict[str, UnannouncedMethod] = Field(description="kapı kimliği → yöntem")
    parcel_method: ParcelMethod
    visitor_fields: VisitorFieldsSchema
    retention_months: int = Field(description="ziyaretçi ve plaka kayıtları (K8; en fazla 24)")
    auto_anonymize: bool = Field(
        description="süre dolanları gece anonimleştir — hukuk onayına kadar varsayılan kapalı"
    )
    parcel_long_wait_days: int
    guest_parking_capacity: int
    version: int = Field(description="0: hiç kaydedilmedi (varsayılanlar)")
    updated_at: dt.datetime | None
    updated_by: str | None


def _settings(state: svc.SettingsState) -> SecuritySettingsOut:
    d = state.data
    return SecuritySettingsOut(
        unannounced_method=d.unannounced_method,
        approval_timeout_seconds=d.approval_timeout_seconds,
        gate_methods=dict(d.gate_methods),
        parcel_method=d.parcel_method,
        visitor_fields=VisitorFieldsSchema(
            plate=d.visitor_fields.plate,
            phone=d.visitor_fields.phone,
            id_seen=d.visitor_fields.id_seen,
        ),
        retention_months=d.retention_months,
        auto_anonymize=d.auto_anonymize,
        parcel_long_wait_days=d.parcel_long_wait_days,
        guest_parking_capacity=d.guest_parking_capacity,
        version=state.version,
        updated_at=state.updated_at,
        updated_by=state.updated_by,
    )


class SecuritySettingsIn(BaseModel):
    unannounced_method: str | None = None
    approval_timeout_seconds: int | None = None
    gate_methods: dict[str, str] = Field(default_factory=dict)
    parcel_method: str | None = None
    visitor_fields: VisitorFieldsSchema = Field(default_factory=VisitorFieldsSchema)
    retention_months: int | None = None
    auto_anonymize: bool = False
    parcel_long_wait_days: int | None = None
    guest_parking_capacity: int | None = None


@router.get("/settings/security", summary="Güvenlik ayarları")
async def get_settings(ctx: SetupRead) -> SecuritySettingsOut:
    return _settings(await svc.load_settings(ctx.session))


@router.put("/settings/security", summary="Güvenlik ayarlarını kaydet (sürüm artar)")
async def put_settings(
    ctx: SetupWrite, body: SecuritySettingsIn, current: CurrentUserDep
) -> Written[SecuritySettingsOut]:
    try:
        data = check_settings(
            unannounced_method=body.unannounced_method,
            approval_timeout_seconds=body.approval_timeout_seconds,
            gate_methods=body.gate_methods,
            parcel_method=body.parcel_method,
            visitor_fields=VisitorFields(**body.visitor_fields.model_dump()),
            retention_months=body.retention_months,
            auto_anonymize=body.auto_anonymize,
            parcel_long_wait_days=body.parcel_long_wait_days,
            guest_parking_capacity=body.guest_parking_capacity,
            gate_ids=await svc.gate_ids(ctx.session),
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    state = await svc.save_settings(ctx.session, data, actor=current.user.full_name)
    await ctx.session.commit()
    return Written(
        data=_settings(state),
        message="Güvenlik ayarları kaydedildi; kapı ekranları hemen güncellenir.",
    )


# --- Kargo teslim yetkilisi ---------------------------------------------------------------


class ParcelDelegateOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    name: str
    created_by: str
    created_at: dt.datetime
    is_active: bool


async def _delegates_out(ctx: SiteContext, rows: list[ParcelDelegate]) -> list[ParcelDelegateOut]:
    names = await security_svc.unit_names(ctx.session, {d.unit_id for d in rows})
    return [
        ParcelDelegateOut(
            id=d.id,
            unit_id=d.unit_id,
            unit_name=names.get(d.unit_id, "—"),
            name=d.name,
            created_by=d.created_by_name,
            created_at=d.created_at,
            is_active=d.is_active,
        )
        for d in rows
    ]


ResidentPackages = Annotated[SiteContext, Depends(resident_access(ModuleKey.PACKAGES))]
GuardPackages = Annotated[
    SiteContext, Depends(access((ModuleKey.PACKAGES,), (Permission.SECURITY_PACKAGES,)))
]


@router.get("/resident/parcel-delegates", summary="Kargo teslim yetkililerim")
async def my_delegates(ctx: ResidentPackages, today: TodayDep) -> list[ParcelDelegateOut]:
    rows = await svc.delegates(ctx.session, await my_unit_ids(ctx, today))
    return await _delegates_out(ctx, rows)


class ParcelDelegateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    unit_id: uuid.UUID | None = Field(default=None, description="boşsa ilk bölümüm")


@router.post(
    "/resident/parcel-delegates", status_code=status.HTTP_201_CREATED, summary="Yetkili ekle"
)
async def add_delegate(
    ctx: ResidentPackages, body: ParcelDelegateIn, current: CurrentUserDep, today: TodayDep
) -> Written[ParcelDelegateOut]:
    unit_id = await my_unit(ctx, today, body.unit_id)
    person = my_person(ctx)
    try:
        row = await svc.add_delegate(
            ctx.session,
            unit_id=unit_id,
            name=body.name,
            person_id=person,
            actor=current.user.full_name,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _delegates_out(ctx, [row])
    return Written(
        data=out,
        message=f"{out.name} kargolarınızı teslim alabilir; kapıda sizin teslim kodunuzu söyler.",
    )


@router.post("/resident/parcel-delegates/{delegate_id}/remove", summary="Yetkiyi kaldır")
async def remove_delegate(
    delegate_id: uuid.UUID,
    ctx: ResidentPackages,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[ParcelDelegateOut]:
    units = await my_unit_ids(ctx, today)
    row = next((d for d in await svc.delegates(ctx.session, units) if d.id == delegate_id), None)
    if row is None:
        raise NotFoundError("Yetkili bulunamadı.")
    await svc.remove_delegate(ctx.session, row, now=now, actor=current.user.full_name)
    await ctx.session.commit()
    [out] = await _delegates_out(ctx, [row])
    return Written(data=out, message=f"{out.name} yetkisi kaldırıldı.")


class DelegateNameOut(BaseModel):
    id: uuid.UUID
    name: str


@router.get("/units/{unit_id}/parcel-delegates", summary="Bölümün teslim yetkilileri (görevli)")
async def unit_delegates(unit_id: uuid.UUID, ctx: GuardPackages) -> list[DelegateNameOut]:
    return [
        DelegateNameOut(id=d.id, name=d.name) for d in await svc.delegates(ctx.session, {unit_id})
    ]


# --- Uzun bekleyen kargolar -----------------------------------------------------------------


class LongWaitingItem(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    carrier: str | None
    received_at: dt.datetime
    waiting_days: int
    delegates: list[str]


class LongWaitingOut(BaseModel):
    days: int = Field(description="ayardaki uzun bekleme günü")
    items: list[LongWaitingItem] = Field(description="en eski üstte; en fazla 200")


@router.get("/packages/long-waiting", summary="Uzun bekleyen kargolar")
async def long_waiting(ctx: GuardPackages, now: NowDep) -> LongWaitingOut:
    days = (await svc.load_settings(ctx.session)).data.parcel_long_wait_days
    rows = await svc.long_waiting(ctx.session, days=days, now=now)
    return LongWaitingOut(
        days=days,
        items=[
            LongWaitingItem(
                id=r.package.id,
                unit_id=r.package.unit_id,
                unit_name=r.unit_name,
                carrier=r.package.carrier,
                received_at=r.package.received_at,
                waiting_days=r.waiting_days,
                delegates=r.delegates,
            )
            for r in rows
        ],
    )
