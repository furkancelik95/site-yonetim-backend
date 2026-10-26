"""Yapı tanımları, bölüm 360° ve kişi işlemleri — servis istekleri 19, 20, 21 (YAP-02..04).

- Ortak alanlar, bölüm tipi ekleme/düzenleme, yürürlük tarihli öznitelikler ve geçmişi, toplu
  düzenleme, yapı özeti.
- Bölüm özeti (`overview`), iletişim görüntüleme kaydı (`contact-views`, kişisel veri erişimi —
  denetim kaydına `contact_view`).
- Mükerrer kişi önerisi, birleştirme (geri alınamaz), malik değişimi (satış / veraset).
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.structure import (
    PersonCreate,
    PersonOut,
    UnitDetail,
    UnitTypeOut,
    _load_unit,
    _rule_error,
    _unit_detail,
    can_contact,
)
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.invitations import HouseholdStatus
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.security import PackageStatus
from site_yonetim.domain.structure import (
    FULL_SHARE,
    SHARE_TOLERANCE,
    CommonAreaKind,
    PartyRole,
    StructureRuleError,
    UnitUsage,
    check_effective_date,
)
from site_yonetim.domain.vehicles import VehicleStatus
from site_yonetim.models import (
    AccountBalance,
    Block,
    CommonArea,
    HouseholdMember,
    LedgerAccount,
    Package,
    Person,
    UnitAttributeChange,
    UnitParty,
    UnitType,
    Vehicle,
)
from site_yonetim.models.audit import AuditAction, current_actor, record
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import people as people_svc
from site_yonetim.services import structure as svc

router = APIRouter(prefix="/sites/{slug}", tags=["site yapısı"])

UnitsRead = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_READ))]
UnitsManage = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
PeopleManage = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_MANAGE))]
PeopleContact = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_CONTACT))]
FORM_ERROR = "Formda düzeltilmesi gereken alanlar var."


def _people_error(exc: people_svc.PeopleRuleError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.fields)


def _actor() -> tuple[str | None, uuid.UUID | None]:
    actor = current_actor()
    return (actor.name, actor.user_id) if actor else (None, None)


# --- Ortak alanlar --------------------------------------------------------------------


class CommonAreaIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    kind: CommonAreaKind | None = None
    block_id: uuid.UUID | None = None
    is_reservable: bool | None = None
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class CommonAreaOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: CommonAreaKind
    block_id: uuid.UUID | None
    block_name: str | None
    is_reservable: bool
    is_active: bool
    note: str | None


async def _area_out(ctx: SiteContext, area: CommonArea) -> CommonAreaOut:
    block_name = None
    if area.block_id is not None:
        block_name = await ctx.session.scalar(select(Block.name).where(Block.id == area.block_id))
    return CommonAreaOut(
        id=area.id,
        name=area.name,
        kind=CommonAreaKind(area.kind),
        block_id=area.block_id,
        block_name=block_name,
        is_reservable=area.is_reservable,
        is_active=area.is_active,
        note=area.note,
    )


@router.get("/common-areas", summary="Ortak alanlar (az sayıda; sayfasız)")
async def list_common_areas(ctx: UnitsRead) -> list[CommonAreaOut]:
    rows = await ctx.session.execute(
        select(CommonArea, Block.name)
        .outerjoin(
            Block, and_(Block.id == CommonArea.block_id, Block.site_id == CommonArea.site_id)
        )
        .order_by(CommonArea.is_active.desc(), CommonArea.name)
    )
    return [
        CommonAreaOut(
            id=a.id,
            name=a.name,
            kind=CommonAreaKind(a.kind),
            block_id=a.block_id,
            block_name=block_name,
            is_reservable=a.is_reservable,
            is_active=a.is_active,
            note=a.note,
        )
        for a, block_name in rows
    ]


async def _save_area(ctx: SiteContext, body: CommonAreaIn, area: CommonArea | None) -> CommonArea:
    data = body.model_dump(exclude_unset=True)
    errors: dict[str, str] = {}
    name = " ".join((data.get("name") or (area.name if area else "")).split())
    if len(name) < 2:
        errors["name"] = "Ortak alanın adını yazın."
    kind = data.get("kind") or (area.kind if area else None)
    if kind is None:
        errors["kind"] = "Tür seçin."
    if data.get("block_id") is not None:
        try:
            await svc.require_block(ctx.session, data["block_id"])
        except StructureRuleError:
            errors["block_id"] = "Blok bulunamadı."
    if not errors.get("name"):
        try:
            await svc.ensure_common_area_name_free(
                ctx.session, name, except_id=area.id if area else None
            )
        except StructureRuleError as exc:
            errors["name"] = exc.message
    if errors:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, errors)
    if area is None:
        area = CommonArea(name=name, kind=str(kind))
        ctx.session.add(area)
    area.name = name
    area.kind = CommonAreaKind(str(kind)).value
    if "block_id" in data:
        area.block_id = data["block_id"]
    for key in ("is_reservable", "is_active"):
        if data.get(key) is not None:
            setattr(area, key, data[key])
    if "note" in data:
        area.note = " ".join((data["note"] or "").split()) or None
    await ctx.session.flush()
    return area


@router.post("/common-areas", status_code=status.HTTP_201_CREATED, summary="Ortak alan ekle")
async def create_common_area(ctx: UnitsManage, body: CommonAreaIn) -> Written[CommonAreaOut]:
    area = await _save_area(ctx, body, None)
    out = await _area_out(ctx, area)
    await ctx.session.commit()
    return Written(data=out, message=f"{out.name} eklendi.")


@router.patch("/common-areas/{area_id}", summary="Ortak alan düzenle / pasifleştir")
async def update_common_area(
    ctx: UnitsManage, area_id: uuid.UUID, body: CommonAreaIn
) -> Written[CommonAreaOut]:
    area = await ctx.session.scalar(select(CommonArea).where(CommonArea.id == area_id))
    if area is None:
        raise NotFoundError("Ortak alan bulunamadı.")
    await _save_area(ctx, body, area)
    out = await _area_out(ctx, area)
    await ctx.session.commit()
    return Written(data=out, message=f"{out.name} güncellendi.")


# --- Bölüm tipi -----------------------------------------------------------------------


class UnitTypeIn(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    weight: Decimal | None = Field(
        default=None, gt=0, le=1000, decimal_places=4, description='katsayı, ör. "1.25"'
    )
    sort_order: int | None = Field(default=None, ge=0, le=1000)


def _type_out(t: UnitType) -> UnitTypeOut:
    return UnitTypeOut(id=t.id, name=t.name, weight=f"{t.weight:.4f}", sort_order=t.sort_order)


async def _save_type(ctx: SiteContext, body: UnitTypeIn, unit_type: UnitType | None) -> UnitType:
    data = body.model_dump(exclude_unset=True)
    name = " ".join((data.get("name") or (unit_type.name if unit_type else "")).split())
    if not name:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {"name": "Tip adını yazın (ör. 2+1, Dükkan)."},
        )
    try:
        await svc.ensure_unit_type_name_free(
            ctx.session, name, except_id=unit_type.id if unit_type else None
        )
    except StructureRuleError as exc:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, {"name": exc.message}
        ) from None
    if unit_type is None:
        count = await ctx.session.scalar(select(func.count()).select_from(UnitType)) or 0
        unit_type = UnitType(name=name, weight=Decimal(1), sort_order=count + 1)
        ctx.session.add(unit_type)
    unit_type.name = name
    if data.get("weight") is not None:
        unit_type.weight = data["weight"]
    if data.get("sort_order") is not None:
        unit_type.sort_order = data["sort_order"]
    await ctx.session.flush()
    return unit_type


@router.post("/unit-types", status_code=status.HTTP_201_CREATED, summary="Bölüm tipi ekle")
async def create_unit_type(ctx: UnitsManage, body: UnitTypeIn) -> Written[UnitTypeOut]:
    unit_type = await _save_type(ctx, body, None)
    await ctx.session.commit()
    return Written(data=_type_out(unit_type), message=f"{unit_type.name} tipi eklendi.")


@router.patch("/unit-types/{type_id}", summary="Bölüm tipi düzenle")
async def update_unit_type(
    ctx: UnitsManage, type_id: uuid.UUID, body: UnitTypeIn
) -> Written[UnitTypeOut]:
    unit_type = await ctx.session.scalar(select(UnitType).where(UnitType.id == type_id))
    if unit_type is None:
        raise NotFoundError("Bölüm tipi bulunamadı.")
    await _save_type(ctx, body, unit_type)
    await ctx.session.commit()
    message = f"{unit_type.name} güncellendi."
    if body.weight is not None:
        message += " Katsayı değişikliği sonraki tahakkuktan itibaren geçerlidir."
    return Written(data=_type_out(unit_type), message=message)


# --- Yürürlük tarihli öznitelikler ----------------------------------------------------


class UnitAttributesIn(BaseModel):
    effective_from: dt.date
    gross_area: Decimal | None = Field(default=None, gt=0, le=100000, decimal_places=2)
    net_area: Decimal | None = Field(default=None, gt=0, le=100000, decimal_places=2)
    land_share_numerator: int | None = Field(default=None, ge=1)
    land_share_denominator: int | None = Field(default=None, ge=1)
    unit_type_id: uuid.UUID | None = None


@router.patch("/units/{unit_id}/attributes", summary="Ölçü, arsa payı, tip (yürürlük tarihli)")
async def update_attributes(
    ctx: UnitsManage, unit_id: uuid.UUID, body: UnitAttributesIn, today: TodayDep, now: NowDep
) -> Written[UnitDetail]:
    """Yalnız gönderilen alanlar değişir. Kapanmış döneme düşen tarih 422 `period_locked`;
    ileri tarih planlanır (`applied=false`) ve tarihi gelince uygulanır. Geçmiş tahakkuk
    değişmez."""
    unit, _, _ = await _load_unit(ctx, unit_id)
    data = body.model_dump(exclude_unset=True)
    effective = data.pop("effective_from")
    try:
        check_effective_date(
            effective, await svc.earliest_effective(ctx.session, today), "effective_from"
        )
        if ("land_share_numerator" in data) != ("land_share_denominator" in data):
            raise StructureRuleError(
                "land_share_incomplete",
                "land_share_numerator",
                "Arsa payı için pay ve payda birlikte girilmeli.",
            )
        if "land_share_numerator" in data:
            svc.check_land_share(data["land_share_numerator"], data["land_share_denominator"])
        if data.get("unit_type_id") is not None:
            await svc.require_unit_type(ctx.session, data["unit_type_id"])
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    changes = svc.attribute_changes(unit, data)
    if not changes:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "no_change", "Değişen bir alan yok.")
    name, user_id = _actor()
    change = await svc.record_attribute_change(
        ctx.session,
        unit,
        changes,
        effective_from=effective,
        today=today,
        now=now,
        actor_name=name,
        user_id=user_id,
    )
    detail = await _unit_detail(ctx, unit.id, today)
    await ctx.session.commit()
    when = effective.strftime("%d.%m.%Y")
    message = (
        f"Bölüm bilgileri {when} itibarıyla güncellendi; geçmiş tahakkuklar değişmez."
        if change.applied
        else f"Değişiklik {when} tarihinde yürürlüğe girecek."
    )
    return Written(data=detail, message=message)


class AttributeChangeOut(BaseModel):
    id: uuid.UUID
    effective_from: dt.date
    changes: dict[str, dict[str, Any]] = Field(
        description='`{"gross_area": {"from": "140.00", "to": "142.50"}}`; tipte ad gösterilir'
    )
    changed_by: str | None
    created_at: dt.datetime
    applied: bool


@router.get("/units/{unit_id}/attribute-history", summary="Öznitelik geçmişi (yeniden eskiye)")
async def attribute_history(ctx: UnitsRead, unit_id: uuid.UUID) -> list[AttributeChangeOut]:
    await _load_unit(ctx, unit_id)
    rows = list(
        await ctx.session.scalars(
            select(UnitAttributeChange)
            .where(UnitAttributeChange.unit_id == unit_id)
            .order_by(
                UnitAttributeChange.effective_from.desc(), UnitAttributeChange.created_at.desc()
            )
        )
    )
    type_names = {
        str(i): n for i, n in await ctx.session.execute(select(UnitType.id, UnitType.name))
    }
    out = []
    for row in rows:
        changes = dict(row.changes)
        if "unit_type_id" in changes:
            change = changes["unit_type_id"]
            changes["unit_type_id"] = {
                "from": type_names.get(change.get("from") or "", change.get("from")),
                "to": type_names.get(change.get("to") or "", change.get("to")),
            }
        out.append(
            AttributeChangeOut(
                id=row.id,
                effective_from=row.effective_from,
                changes=changes,
                changed_by=row.changed_by_name,
                created_at=row.created_at,
                applied=row.applied,
            )
        )
    return out


# --- Toplu düzenleme --------------------------------------------------------------------


class BulkChangesIn(BaseModel):
    unit_type_id: uuid.UUID | None = None
    usage: UnitUsage | None = None
    is_active: bool | None = None


class BulkUpdateIn(BaseModel):
    unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    changes: BulkChangesIn


class BulkFailureOut(BaseModel):
    unit_id: uuid.UUID
    message: str


class BulkUpdateOut(BaseModel):
    updated: int
    failed: list[BulkFailureOut]


@router.post("/units/bulk-update", summary="Toplu düzenleme: tip, kullanım, durum")
async def bulk_update(
    ctx: UnitsManage, body: BulkUpdateIn, today: TodayDep, now: NowDep
) -> Written[BulkUpdateOut]:
    """m² ve arsa payı toplu değişmez (yürürlük tarihi ister). Tip değişikliği bugünden
    geçerli geçmiş satırı yazar. Satır satır uygulanır; başarısızlar `failed` ile döner."""
    if not body.unit_ids:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "En az bir bölüm seçin.",
            {"unit_ids": "En az bir bölüm seçin."},
        )
    raw = body.changes.model_dump(exclude_unset=True)
    changes = {k: v for k, v in raw.items() if k != "unit_type_id" and v is not None}
    set_type = "unit_type_id" in raw  # null: tipi kaldır
    type_id = raw.get("unit_type_id")
    if not changes and not set_type:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "Değiştirilecek alanı seçin.",
            {"changes": "En az bir alan seçin."},
        )
    if set_type:
        try:
            if type_id is not None:
                await svc.require_unit_type(ctx.session, type_id)
            check_effective_date(
                today, await svc.earliest_effective(ctx.session, today), "effective_from"
            )
        except StructureRuleError as exc:
            raise _rule_error(exc) from None
    if "usage" in changes:
        changes["usage"] = changes["usage"].value
    updated, failed = await svc.bulk_update(ctx.session, body.unit_ids, changes)
    if set_type:
        name, user_id = _actor()
        failed_ids = {f.unit_id for f in failed}
        for unit_id in dict.fromkeys(body.unit_ids):
            if unit_id in failed_ids:
                continue
            unit, _, _ = await _load_unit(ctx, unit_id)
            diff = svc.attribute_changes(unit, {"unit_type_id": type_id})
            if diff:
                await svc.record_attribute_change(
                    ctx.session, unit, diff, effective_from=today, today=today, now=now,
                    actor_name=name, user_id=user_id,
                )  # fmt: skip
    await ctx.session.commit()
    message = f"{updated} bölüm güncellendi."
    if failed:
        message = f"{updated} bölüm güncellendi, {len(failed)} bölümde hata."
    return Written(
        data=BulkUpdateOut(
            updated=updated,
            failed=[BulkFailureOut(unit_id=f.unit_id, message=f.message) for f in failed],
        ),
        message=message,
    )


# --- Yapı özeti -----------------------------------------------------------------------


class UnitRefOut(BaseModel):
    id: uuid.UUID
    display_name: str


class ShareGapOut(UnitRefOut):
    total: str = Field(description='malik hisse toplamı, "50.00"')


class StructureSummaryOut(BaseModel):
    unit_count: int
    active_unit_count: int
    block_count: int
    land_share_sum: int
    land_share_denominator: int | None = Field(description="etkin bölümlerde en sık payda")
    land_share_mismatch: bool
    units_without_land_share: int
    units_without_owner: list[UnitRefOut]
    units_shares_incomplete: list[ShareGapOut]
    common_area_count: int


@router.get("/structure/summary", summary="Yapı özeti ve uyarılar")
async def structure_summary(ctx: UnitsRead, today: TodayDep) -> StructureSummaryOut:
    s = await svc.structure_summary(ctx.session, today)
    return StructureSummaryOut(
        unit_count=s.unit_count,
        active_unit_count=s.active_unit_count,
        block_count=s.block_count,
        land_share_sum=s.land_share_sum,
        land_share_denominator=s.land_share_denominator,
        land_share_mismatch=s.land_share_mismatch,
        units_without_land_share=s.units_without_land_share,
        units_without_owner=[
            UnitRefOut(id=u.id, display_name=u.display_name) for u in s.units_without_owner
        ],
        units_shares_incomplete=[
            ShareGapOut(id=u.id, display_name=u.display_name, total=f"{u.total:.2f}")
            for u in s.units_shares_incomplete
        ],
        common_area_count=s.common_area_count,
    )


# --- Bölüm 360° özeti -------------------------------------------------------------------


class UnitOverviewOut(BaseModel):
    balance: Money | None = Field(
        description="açık cari hesapların bakiye toplamı; finans izni yoksa null"
    )
    share_total: str = Field(description='yürürlükteki maliklerin hisse toplamı, "100.00"')
    warnings: list[Literal["no_owner", "shares_incomplete", "no_land_share"]]
    packages_waiting: int
    vehicles: int
    household: int


@router.get("/units/{unit_id}/overview", summary="Bölüm kartı özeti")
async def unit_overview(ctx: UnitsRead, unit_id: uuid.UUID, today: TodayDep) -> UnitOverviewOut:
    unit, _, _ = await _load_unit(ctx, unit_id)
    session = ctx.session
    balance = None
    if ctx.access.can(Permission.FINANCE_READ):
        balance = await session.scalar(
            select(func.coalesce(func.sum(AccountBalance.balance), 0))
            .select_from(LedgerAccount)
            .outerjoin(
                AccountBalance,
                and_(
                    AccountBalance.account_id == LedgerAccount.id,
                    AccountBalance.site_id == LedgerAccount.site_id,
                ),
            )
            .where(LedgerAccount.unit_id == unit.id, LedgerAccount.is_closed.is_(False))
        )
        balance = Decimal(balance or 0)
    shares = await session.execute(
        select(func.count(), func.coalesce(func.sum(UnitParty.share_percent), 0)).where(
            UnitParty.unit_id == unit.id,
            UnitParty.role == PartyRole.OWNER.value,
            UnitParty.start_date <= today,
            (UnitParty.end_date.is_(None)) | (UnitParty.end_date >= today),
        )
    )
    owner_count, share_total = shares.one()
    share_total = Decimal(share_total)
    warnings: list[Literal["no_owner", "shares_incomplete", "no_land_share"]] = []
    if not owner_count:
        warnings.append("no_owner")
    elif abs(share_total - FULL_SHARE) > SHARE_TOLERANCE:
        warnings.append("shares_incomplete")
    if unit.land_share_numerator is None:
        warnings.append("no_land_share")
    packages = 0
    if ModuleKey.PACKAGES in ctx.modules:
        packages = (
            await session.scalar(
                select(func.count())
                .select_from(Package)
                .where(Package.unit_id == unit.id, Package.status == PackageStatus.WAITING.value)
            )
            or 0
        )
    vehicles = (
        await session.scalar(
            select(func.count())
            .select_from(Vehicle)
            .where(Vehicle.unit_id == unit.id, Vehicle.status != VehicleStatus.INACTIVE.value)
        )
        or 0
    )
    household = (
        await session.scalar(
            select(func.count())
            .select_from(HouseholdMember)
            .where(
                HouseholdMember.unit_id == unit.id,
                HouseholdMember.status != HouseholdStatus.ENDED.value,
            )
        )
        or 0
    )
    return UnitOverviewOut(
        balance=balance,
        share_total=f"{share_total:.2f}",
        warnings=warnings,
        packages_waiting=packages,
        vehicles=vehicles,
        household=household,
    )


# --- İletişim görüntüleme (kişisel veri erişim kaydı) -----------------------------------


class ContactOut(BaseModel):
    phone: str | None
    email: str | None


@router.post("/people/{person_id}/contact-views", summary="Maskeli iletişimi aç (denetimli)")
async def view_contact(ctx: PeopleContact, person_id: uuid.UUID) -> Written[ContactOut]:
    """Açık değeri döner ve denetim kaydına `contact_view` (kim, hangi kişi, ne zaman) yazar.
    Denetim satırına değerin kendisi yazılmaz."""
    person = await ctx.session.scalar(
        select(Person).where(Person.id == person_id, Person.merged_into_id.is_(None))
    )
    if person is None:
        raise NotFoundError("Kişi bulunamadı.")
    record(
        ctx.session, AuditAction.CONTACT_VIEW, "persons", person.id,
        {"fields": [k for k in ("phone", "email") if getattr(person, k)]},
        reason="Kişisel veri görüntüleme",
    )  # fmt: skip
    out = ContactOut(phone=person.phone, email=person.email)
    await ctx.session.commit()
    return Written(
        data=out, message="İletişim bilgisi açıldı; görüntüleme denetim kaydına yazıldı."
    )


# --- Mükerrer kişi ve birleştirme -------------------------------------------------------


class DuplicateGroupOut(BaseModel):
    key: str = Field(description="maskeli telefon ya da e-posta")
    kind: Literal["phone", "email"]
    people: list[PersonOut]


@router.get("/people/duplicates", summary="Mükerrer olabilecek kişiler (öneri)")
async def people_duplicates(ctx: PeopleManage) -> list[DuplicateGroupOut]:
    contact = can_contact(ctx)
    groups = await people_svc.duplicates(ctx.session)
    out: list[DuplicateGroupOut] = []
    for g in groups:
        people = [PersonOut.of(p, contact=contact) for p in g.people]
        masked = people[0].phone if g.kind == "phone" else people[0].email
        out.append(
            DuplicateGroupOut(
                key=(masked or "—") if contact else f"{g.kind}-{len(out) + 1}",
                kind=g.kind,  # type: ignore[arg-type]
                people=people,
            )
        )
    return out


class MergeIn(BaseModel):
    keep_id: uuid.UUID
    merge_id: uuid.UUID


class MergeOut(BaseModel):
    kept: PersonOut
    merged: PersonOut


@router.post("/people/merge", summary="İki kişi kaydını birleştir (geri alınamaz)")
async def merge_people(ctx: PeopleManage, body: MergeIn, now: NowDep) -> Written[MergeOut]:
    try:
        merged = await people_svc.merge(
            ctx.session, keep_id=body.keep_id, merge_id=body.merge_id, now=now
        )
    except people_svc.PeopleRuleError as exc:
        raise _people_error(exc) from None
    record(
        ctx.session, AuditAction.UPDATE, "persons", merged.kept.id,
        {"merged_person_id": str(merged.merged.id), "moved": merged.moved},
        reason="Kişi birleştirme (geri alınamaz)",
    )  # fmt: skip
    contact = can_contact(ctx)
    out = MergeOut(
        kept=PersonOut.of(merged.kept, contact=contact),
        merged=PersonOut.of(merged.merged, contact=contact),
    )
    await ctx.session.commit()
    return Written(
        data=out,
        message=(
            f"{out.merged.full_name} kaydı {out.kept.full_name} ile birleştirildi; ilişkiler, "
            "cari hesaplar ve geçmiş tek kişide toplandı."
        ),
    )


# --- Malik değişimi -----------------------------------------------------------------------


class NewOwnerIn(BaseModel):
    person_id: uuid.UUID | None = None
    person: PersonCreate | None = None
    share_percent: Decimal = Field(gt=0, le=100, decimal_places=2)

    @model_validator(mode="after")
    def _one(self) -> NewOwnerIn:
        if (self.person_id is None) == (self.person is None):
            raise ValueError("Kayıtlı kişi (person_id) ya da yeni kişi (person) verin.")
        return self


class OwnershipTransferIn(BaseModel):
    reason: Literal["sale", "inheritance"]
    transfer_date: dt.date
    debt_policy: str = "stays_with_seller"
    new_owners: list[NewOwnerIn] = Field(default_factory=list, max_length=20)

    @field_validator("debt_policy")
    @classmethod
    def _policy(cls, value: str) -> str:
        return value.strip()


class OwnershipTransferOut(BaseModel):
    status: Literal["done", "approval_pending"]
    unit: UnitDetail | None = Field(description="işlem yapıldıysa güncel bölüm")
    approval_request_id: uuid.UUID | None


@router.post("/units/{unit_id}/ownership-transfer", summary="Malik değişimi (satış / veraset)")
async def ownership_transfer(
    ctx: UnitsManage,
    unit_id: uuid.UUID,
    body: OwnershipTransferIn,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[OwnershipTransferOut]:
    """Tek işlemde: eski malikler devirden bir gün önce kapanır, yeniler devir günü başlar;
    hisse toplamı tam %100. Devir öncesi borç eski malikte kalır (K10). Onay kuralı
    (`ownership_transfer`) tanımlıysa 202 `approval_pending`."""
    if body.debt_policy != "stays_with_seller":
        message = (
            "Satış öncesi borcun yeni malike devri hukuki teyit bekliyor (K10); şimdilik borç "
            "eski malikte kalır."
        )
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "not_supported",
            message,
            {"debt_policy": 'Yalnız "eski malikte kalır" seçilebilir.'},
        )
    if not ctx.access.can(Permission.PEOPLE_MANAGE) and any(o.person for o in body.new_owners):
        raise ForbiddenError("Yeni kişi eklemek için kişi yönetimi yetkisi gerekir.")
    unit, block, _ = await _load_unit(ctx, unit_id)
    owners = [
        people_svc.NewOwner(
            share_percent=o.share_percent,
            person_id=o.person_id,
            first_name=o.person.first_name if o.person else None,
            last_name=o.person.last_name if o.person else None,
            phone=o.person.phone if o.person else None,
            email=o.person.email if o.person else None,
        )
        for o in body.new_owners
    ]
    label = people_svc.REASON_LABELS[body.reason]
    try:
        await people_svc.check_transfer(ctx.session, owners, body.transfer_date, today)
        version = await approvals_svc.match_rule(
            ctx.session, ActionType.OWNERSHIP_TRANSFER, amount=None, source=None, today=today
        )
        if version is not None:
            request = await approvals_svc.request_approval(
                ctx.session,
                ctx.site,
                approvals_svc.NewRequest(
                    action_type=ActionType.OWNERSHIP_TRANSFER,
                    title=f"Malik değişimi ({label}) — {unit.display_name(block.name)}",
                    summary={
                        "unit": unit.display_name(block.name),
                        "reason": body.reason,
                        "transfer_date": body.transfer_date.isoformat(),
                        "new_owner_count": len(owners),
                    },
                    payload={
                        "unit_id": str(unit.id),
                        "reason": body.reason,
                        "transfer_date": body.transfer_date.isoformat(),
                        "new_owners": [o.payload() for o in owners],
                    },
                    subject_type="units",
                    subject_id=unit.id,
                ),
                requester=approvals_svc.Person(current.user.id, current.user.full_name),
                today=today,
                now=now,
            )
            await ctx.session.commit()
            response.status_code = HTTPStatus.ACCEPTED
            return Written(
                data=OwnershipTransferOut(
                    status="approval_pending", unit=None, approval_request_id=request.id
                ),
                message="Malik değişimi onaya gönderildi; onaylanınca uygulanır.",
            )
        closed = await people_svc.transfer(
            ctx.session, unit, block, owners=owners, transfer_date=body.transfer_date, today=today
        )
    except people_svc.PeopleRuleError as exc:
        raise _people_error(exc) from None
    except approvals_svc.ApprovalError as exc:  # pragma: no cover — yürütücü kayıtlı
        raise ApiError(exc.status, exc.code, exc.message) from None
    record(
        ctx.session, AuditAction.UPDATE, "units", unit.id,
        {"ownership_transfer": body.reason, "transfer_date": body.transfer_date.isoformat(),
         "closed_owners": closed, "new_owners": len(owners), "debt_policy": body.debt_policy},
    )  # fmt: skip
    detail = await _unit_detail(ctx, unit.id, today)
    await ctx.session.commit()
    when = body.transfer_date.strftime("%d.%m.%Y")
    return Written(
        data=OwnershipTransferOut(status="done", unit=detail, approval_request_id=None),
        message=(
            f"{detail.display_name}: {label} kaydedildi; eski malik ilişkisi {when} öncesinde "
            f"kapandı, {len(owners)} yeni malik başladı. Satış öncesi borç eski malikte kaldı."
        ),
    )
