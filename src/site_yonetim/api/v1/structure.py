"""Site yapısı uçları: blok, daire tipi, bölüm, kişi, bölüm–kişi ilişkisi (docs/06 §2.4).

Her uç `site_context` (erişim 404, site kapsamı) + izin kontrolünden (403) geçer. Başka sitenin
kimliği kiracı filtresi yüzünden görünmez: yol parametresindeyse 404, gövdedeyse 422.
Kişisel veri (telefon, e-posta) yalnız `people.read` iznine gösterilir (docs/09 §5).
"""

import uuid
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Select, func, select

from site_yonetim.api.deps import SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.structure import PartyRole, StructureRuleError
from site_yonetim.domain.text import normalize_person_name
from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_name_part,
    normalize_tr_mobile,
)
from site_yonetim.models import Block, LedgerAccount, Person, Unit, UnitParty, UnitType, UnitUsage
from site_yonetim.services import structure as svc

router = APIRouter(prefix="/sites/{slug}", tags=["site yapısı"])

UnitsRead = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_READ))]
UnitsManage = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
PeopleRead = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_READ))]
PeopleManage = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_MANAGE))]
Paging = Annotated[PageParams, Depends()]

_CONFLICT_CODES = {"party_already_active", "owner_shares_exceed", "party_already_ended"}


def _rule_error(exc: StructureRuleError) -> ApiError:
    if exc.code.endswith("already_exists") or exc.code in _CONFLICT_CODES:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


async def _page(
    ctx: SiteContext, query: Select[*tuple[Any, ...]], params: PageParams
) -> tuple[list[Any], int]:
    """Toplam veritabanında sayılır; yalnız istenen sayfa çekilir (docs/08 §3)."""
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.execute(query.offset(params.offset).limit(params.page_size))
    return list(rows), total


# --- Blok ---------------------------------------------------------------------


class BlockOut(BaseModel):
    id: uuid.UUID
    name: str
    has_elevator: bool
    floor_count: int | None
    sort_order: int
    unit_count: int = 0

    @classmethod
    def of(cls, block: Block, unit_count: int = 0) -> BlockOut:
        return cls(
            id=block.id,
            name=block.name,
            has_elevator=block.has_elevator,
            floor_count=block.floor_count,
            sort_order=block.sort_order,
            unit_count=unit_count,
        )


class BlockCreate(BaseModel):
    name: str = Field(max_length=40, description="Tek bloklu sitede boş olabilir")
    has_elevator: bool = False
    floor_count: int | None = Field(default=None, ge=1, le=100)
    sort_order: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def _collapse(cls, value: str) -> str:
        return " ".join(value.split())


class BlockUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    has_elevator: bool | None = None
    floor_count: int | None = Field(default=None, ge=1, le=100)
    sort_order: int | None = Field(default=None, ge=0)


@router.get("/blocks", summary="Bloklar")
async def list_blocks(ctx: UnitsRead, params: Paging) -> Page[BlockOut]:
    counts = select(Unit.block_id, func.count().label("n")).group_by(Unit.block_id).subquery()
    query = (
        select(Block, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.block_id == Block.id)
        .order_by(Block.sort_order, Block.name)
    )
    rows, total = await _page(ctx, query, params)
    return Page(
        items=[BlockOut.of(block, n) for block, n in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


@router.post("/blocks", status_code=status.HTTP_201_CREATED, summary="Blok ekle")
async def create_block(ctx: UnitsManage, body: BlockCreate) -> Written[BlockOut]:
    try:
        await svc.ensure_block_name_free(ctx.session, body.name)
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    block = Block(**body.model_dump())
    ctx.session.add(block)
    await ctx.session.commit()
    label = f"{block.name} bloğu" if block.name else "Blok"
    return Written(data=BlockOut.of(block), message=f"{label} eklendi.")


@router.patch("/blocks/{block_id}", summary="Blok güncelle")
async def update_block(
    ctx: UnitsManage, block_id: uuid.UUID, body: BlockUpdate
) -> Written[BlockOut]:
    block = await svc.get_block(ctx.session, block_id)
    if block is None:
        raise NotFoundError
    changes = body.model_dump(exclude_unset=True)
    try:
        if "name" in changes:
            if changes["name"] is None:
                raise StructureRuleError("invalid_block_name", "name", "Blok adı boş bırakılamaz.")
            changes["name"] = " ".join(changes["name"].split())
            await svc.ensure_block_name_free(ctx.session, changes["name"], except_id=block.id)
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    for key, value in changes.items():
        setattr(block, key, value)
    await ctx.session.commit()
    return Written(data=BlockOut.of(block), message="Blok güncellendi.")


# --- Daire tipi ---------------------------------------------------------------


class UnitTypeOut(BaseModel):
    id: uuid.UUID
    name: str
    weight: str = Field(description='Ağırlık, metin (ör. "1.3500")')
    sort_order: int


@router.get("/unit-types", summary="Daire tipleri")
async def list_unit_types(ctx: UnitsRead, params: Paging) -> Page[UnitTypeOut]:
    query = select(UnitType).order_by(UnitType.sort_order, UnitType.name)
    rows, total = await _page(ctx, query, params)
    return Page(
        items=[
            UnitTypeOut(id=t.id, name=t.name, weight=f"{t.weight:f}", sort_order=t.sort_order)
            for (t,) in rows
        ],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


# --- Bölüm --------------------------------------------------------------------


class NamedRef(BaseModel):
    id: uuid.UUID
    name: str


class UnitListItem(BaseModel):
    id: uuid.UUID
    display_name: str
    block: NamedRef
    number: str
    floor: int | None
    unit_type: NamedRef | None
    gross_area: str | None = Field(description="m², metin")
    net_area: str | None
    land_share_numerator: int | None
    land_share_denominator: int | None
    usage: str
    is_active: bool
    commercial_title: str | None
    owner_names: list[str]
    tenant_names: list[str]


def _area(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _unit_item(
    unit: Unit, block: Block, unit_type: UnitType | None, parties: svc.CurrentParties
) -> UnitListItem:
    return UnitListItem(
        id=unit.id,
        display_name=unit.display_name(block.name),
        block=NamedRef(id=block.id, name=block.name),
        number=unit.number,
        floor=unit.floor,
        unit_type=NamedRef(id=unit_type.id, name=unit_type.name) if unit_type else None,
        gross_area=_area(unit.gross_area),
        net_area=_area(unit.net_area),
        land_share_numerator=unit.land_share_numerator,
        land_share_denominator=unit.land_share_denominator,
        usage=unit.usage,
        is_active=unit.is_active,
        commercial_title=unit.commercial_title,
        owner_names=[p.full_name for p in parties.owners],
        tenant_names=[p.full_name for p in parties.tenants],
    )


@router.get("/units", summary="Bağımsız bölümler (sayfalı)")
async def list_units(
    ctx: UnitsRead,
    params: Paging,
    today: TodayDep,
    q: Annotated[str | None, Query(max_length=60, description="bölüm no / blok+no / unvan")] = None,
    block_id: uuid.UUID | None = None,
    is_active: bool | None = None,
) -> Page[UnitListItem]:
    query = svc.units_query(block_id=block_id, is_active=is_active, q=q)
    rows, total = await _page(ctx, query, params)
    parties = await svc.current_parties(ctx.session, [u.id for u, _, _ in rows], today)
    return Page(
        items=[_unit_item(u, b, t, parties[u.id]) for u, b, t in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


class PersonOut(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    full_name: str
    phone: str | None = Field(description="Yalnız people.read izniyle; aksi halde null")
    email: str | None = Field(description="Yalnız people.read izniyle; aksi halde null")

    @classmethod
    def of(cls, person: Person, *, contact: bool) -> PersonOut:
        return cls(
            id=person.id,
            first_name=person.first_name,
            last_name=person.last_name,
            full_name=person.full_name,
            phone=person.phone if contact else None,
            email=person.email if contact else None,
        )


class PartyOut(BaseModel):
    id: uuid.UUID
    role: str
    share_percent: str
    start_date: date
    end_date: date | None
    is_current: bool
    person: PersonOut


class AccountOut(BaseModel):
    id: uuid.UUID
    kind: str
    reference_code: str
    person_id: uuid.UUID
    is_closed: bool

    @classmethod
    def of(cls, account: LedgerAccount) -> AccountOut:
        return cls(
            id=account.id,
            kind=account.kind,
            reference_code=account.reference_code,
            person_id=account.person_id,
            is_closed=account.is_closed,
        )


class UnitDetail(UnitListItem):
    parties: list[PartyOut] = Field(description="Geçmiş dahil, yeniden eskiye")
    accounts: list[AccountOut]


async def _load_unit(ctx: SiteContext, unit_id: uuid.UUID) -> tuple[Unit, Block, UnitType | None]:
    row = (
        await ctx.session.execute(
            svc.units_query(block_id=None, is_active=None, q=None).where(Unit.id == unit_id)
        )
    ).first()
    if row is None:
        raise NotFoundError
    return row[0], row[1], row[2]


async def _unit_detail(ctx: SiteContext, unit_id: uuid.UUID, today: date) -> UnitDetail:
    unit, block, unit_type = await _load_unit(ctx, unit_id)
    current = (await svc.current_parties(ctx.session, [unit.id], today))[unit.id]
    contact = ctx.access.can(Permission.PEOPLE_READ)
    party_rows = await ctx.session.execute(
        select(UnitParty, Person)
        .join(Person, Person.id == UnitParty.person_id)
        .where(UnitParty.unit_id == unit.id)
        .order_by(UnitParty.start_date.desc(), UnitParty.role)
    )
    parties = [
        PartyOut(
            id=party.id,
            role=party.role,
            share_percent=f"{party.share_percent:.2f}",
            start_date=party.start_date,
            end_date=party.end_date,
            is_current=party.start_date <= today
            and (party.end_date is None or party.end_date >= today),
            person=PersonOut.of(person, contact=contact),
        )
        for party, person in party_rows
    ]
    accounts = [AccountOut.of(a) for a in await svc.unit_accounts(ctx.session, unit.id)]
    item = _unit_item(unit, block, unit_type, current)
    return UnitDetail(**item.model_dump(), parties=parties, accounts=accounts)


@router.get("/units/{unit_id}", summary="Bölüm ayrıntısı: taraflar (geçmiş dahil) ve hesaplar")
async def get_unit(ctx: UnitsRead, unit_id: uuid.UUID, today: TodayDep) -> UnitDetail:
    return await _unit_detail(ctx, unit_id, today)


class UnitFields(BaseModel):
    number: str | None = Field(default=None, min_length=1, max_length=20)
    floor: int | None = Field(default=None, ge=-5, le=100)
    unit_type_id: uuid.UUID | None = None
    gross_area: Decimal | None = Field(default=None, gt=0, le=100000, decimal_places=2)
    net_area: Decimal | None = Field(default=None, gt=0, le=100000, decimal_places=2)
    land_share_numerator: int | None = Field(default=None, ge=1)
    land_share_denominator: int | None = Field(default=None, ge=1)
    usage: UnitUsage | None = None
    commercial_title: str | None = Field(default=None, max_length=150)

    @field_validator("number")
    @classmethod
    def _number(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class UnitCreate(UnitFields):
    block_id: uuid.UUID
    number: str = Field(min_length=1, max_length=20)


class UnitUpdate(UnitFields):
    block_id: uuid.UUID | None = None
    is_active: bool | None = Field(default=None, description="false: pasif (silme yerine)")


async def _validate_unit(ctx: SiteContext, data: dict[str, Any], unit: Unit | None) -> None:
    session = ctx.session
    block_id: uuid.UUID | None = data.get("block_id", unit.block_id if unit else None)
    number: str | None = data.get("number", unit.number if unit else None)
    if "block_id" in data:
        await svc.require_block(session, data["block_id"])
    if data.get("unit_type_id") is not None:
        await svc.require_unit_type(session, data["unit_type_id"])
    if ("block_id" in data or "number" in data) and block_id and number:
        await svc.ensure_unit_number_free(
            session,
            block_id,
            number,
            except_id=unit.id if unit else None,
        )
    num = data.get("land_share_numerator", unit.land_share_numerator if unit else None)
    den = data.get("land_share_denominator", unit.land_share_denominator if unit else None)
    svc.check_land_share(num, den)


@router.post("/units", status_code=status.HTTP_201_CREATED, summary="Bölüm ekle")
async def create_unit(ctx: UnitsManage, body: UnitCreate, today: TodayDep) -> Written[UnitDetail]:
    data = body.model_dump(exclude_none=True)
    data["usage"] = (body.usage or UnitUsage.RESIDENTIAL).value
    try:
        await _validate_unit(ctx, data, None)
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    unit = Unit(**data)
    ctx.session.add(unit)
    await ctx.session.flush()
    detail = await _unit_detail(ctx, unit.id, today)
    await ctx.session.commit()
    return Written(
        data=detail,
        message=f"{detail.display_name} bölümü eklendi. Sıradaki adım: malikini ekleyin.",
    )


@router.patch("/units/{unit_id}", summary="Bölüm güncelle / pasifleştir")
async def update_unit(
    ctx: UnitsManage, unit_id: uuid.UUID, body: UnitUpdate, today: TodayDep
) -> Written[UnitDetail]:
    unit, _, _ = await _load_unit(ctx, unit_id)
    data = body.model_dump(exclude_unset=True)
    for required in ("block_id", "number", "usage", "is_active"):
        if required in data and data[required] is None:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                "Gönderilen bilgilerde hata var.",
                {required: "Bu alan boş bırakılamaz."},
            )
    if "usage" in data:
        data["usage"] = data["usage"].value
    try:
        await _validate_unit(ctx, data, unit)
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    for key, value in data.items():
        setattr(unit, key, value)
    await ctx.session.flush()
    detail = await _unit_detail(ctx, unit.id, today)
    await ctx.session.commit()
    verb = "pasifleştirildi" if data.get("is_active") is False else "güncellendi"
    return Written(data=detail, message=f"{detail.display_name} bölümü {verb}.")


# --- Kişi ---------------------------------------------------------------------


class PersonFields(BaseModel):
    phone: str | None = None
    email: str | None = Field(default=None, max_length=254)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_tr_mobile(value) if value else None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return normalize_email_address(value) if value else None


class PersonCreate(PersonFields):
    first_name: str
    last_name: str

    @model_validator(mode="after")
    def _names(self) -> PersonCreate:
        first = normalize_name_part(self.first_name, "Ad")
        last = normalize_name_part(self.last_name, "Soyad")
        self.first_name, self.last_name = normalize_person_name(first, last)
        return self


class PersonUpdate(PersonFields):
    first_name: str | None = None
    last_name: str | None = None


@router.get("/people", summary="Kişiler (sayfalı)")
async def list_people(
    ctx: PeopleRead,
    params: Paging,
    q: Annotated[str | None, Query(max_length=80, description="ad soyad içinde arar")] = None,
) -> Page[PersonOut]:
    rows, total = await _page(ctx, svc.people_query(q), params)
    return Page(
        items=[PersonOut.of(p, contact=True) for (p,) in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


@router.post("/people", status_code=status.HTTP_201_CREATED, summary="Kişi ekle")
async def create_person(ctx: PeopleManage, body: PersonCreate) -> Written[PersonOut]:
    person = Person(**body.model_dump())
    ctx.session.add(person)
    await ctx.session.commit()
    return Written(data=PersonOut.of(person, contact=True), message=f"{person.full_name} eklendi.")


@router.patch("/people/{person_id}", summary="Kişi güncelle")
async def update_person(
    ctx: PeopleManage, person_id: uuid.UUID, body: PersonUpdate
) -> Written[PersonOut]:
    person = await ctx.session.scalar(select(Person).where(Person.id == person_id))
    if person is None:
        raise NotFoundError
    data = body.model_dump(exclude_unset=True)
    first = data.pop("first_name", None) or person.first_name
    last = data.pop("last_name", None) or person.last_name
    try:
        first = normalize_name_part(first, "Ad")
        last = normalize_name_part(last, "Soyad")
    except ValueError as exc:
        field = "first_name" if str(exc).startswith("Ad") else "last_name"
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", str(exc), {field: str(exc)}
        ) from None
    person.first_name, person.last_name = normalize_person_name(first, last)
    for key, value in data.items():
        setattr(person, key, value)
    await ctx.session.commit()
    return Written(
        data=PersonOut.of(person, contact=True), message=f"{person.full_name} güncellendi."
    )


# --- Bölüm–kişi ilişkisi ------------------------------------------------------


class PartyCreate(BaseModel):
    role: PartyRole
    start_date: date
    share_percent: Decimal = Field(default=Decimal(100), gt=0, le=100, decimal_places=2)
    person_id: uuid.UUID | None = Field(default=None, description="Mevcut kişi; ya da `person`")
    person: PersonCreate | None = Field(default=None, description="Yeni kişi")

    @model_validator(mode="after")
    def _one_person(self) -> PartyCreate:
        if (self.person_id is None) == (self.person is None):
            raise ValueError(
                "Mevcut bir kişi (person_id) ya da yeni kişi bilgisi (person) verin; "
                "ikisi birden değil."
            )
        return self


class PartyAdded(BaseModel):
    party: PartyOut
    opened_accounts: list[AccountOut]


_ROLE_LABEL = {
    PartyRole.OWNER: "malik",
    PartyRole.TENANT: "kiracı",
    PartyRole.RESIDENT: "oturan",
    PartyRole.PROXY: "vekil",
}


@router.post(
    "/units/{unit_id}/parties", status_code=status.HTTP_201_CREATED, summary="Malik/kiracı ekle"
)
async def add_party(
    ctx: PeopleManage, unit_id: uuid.UUID, body: PartyCreate, today: TodayDep
) -> Written[PartyAdded]:
    """Gerekli cari hesapları da açar: malik `-M` (kiracı yoksa `-O`), kiracı `-K` (docs/03 §5)."""
    unit, block, _ = await _load_unit(ctx, unit_id)
    try:
        if body.person_id is not None:
            person = await svc.require_person(ctx.session, body.person_id)
        elif body.person is not None:
            person = Person(**body.person.model_dump())
            ctx.session.add(person)
            await ctx.session.flush()
        else:  # pragma: no cover — model doğrulayıcısı engeller
            raise StructureRuleError("person_required", "person", "Kişi bilgisi gerekli.")
        added = await svc.add_party(
            ctx.session,
            unit=unit,
            block=block,
            person=person,
            role=body.role,
            start_date=body.start_date,
            share_percent=body.share_percent,
        )
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    await ctx.session.commit()
    party = added.party
    codes = ", ".join(a.reference_code for a in added.opened_accounts)
    accounts_text = f"; {len(added.opened_accounts)} cari hesap açıldı ({codes})" if codes else ""
    return Written(
        data=PartyAdded(
            party=PartyOut(
                id=party.id,
                role=party.role,
                share_percent=f"{party.share_percent:.2f}",
                start_date=party.start_date,
                end_date=party.end_date,
                is_current=party.start_date <= today,
                person=PersonOut.of(person, contact=True),
            ),
            opened_accounts=[AccountOut.of(a) for a in added.opened_accounts],
        ),
        message=(
            f"{unit.display_name(block.name)} — {person.full_name} "
            f"{_ROLE_LABEL[body.role]} olarak eklendi{accounts_text}."
        ),
    )


class PartyEnd(BaseModel):
    end_date: date


@router.post("/units/{unit_id}/parties/{party_id}/end", summary="İlişkiyi sona erdir (silmez)")
async def end_party(
    ctx: PeopleManage, unit_id: uuid.UUID, party_id: uuid.UUID, body: PartyEnd, today: TodayDep
) -> Written[PartyOut]:
    row = (
        await ctx.session.execute(
            select(UnitParty, Person)
            .join(Person, Person.id == UnitParty.person_id)
            .where(UnitParty.id == party_id, UnitParty.unit_id == unit_id)
        )
    ).first()
    if row is None:
        raise NotFoundError
    party, person = row
    try:
        await svc.end_party(ctx.session, party, body.end_date)
    except StructureRuleError as exc:
        raise _rule_error(exc) from None
    await ctx.session.commit()
    return Written(
        data=PartyOut(
            id=party.id,
            role=party.role,
            share_percent=f"{party.share_percent:.2f}",
            start_date=party.start_date,
            end_date=party.end_date,
            is_current=party.start_date <= today and (party.end_date or today) >= today,
            person=PersonOut.of(person, contact=True),
        ),
        message=f"{person.full_name} için bitiş tarihi kaydedildi; geçmiş kayıt korunur.",
    )
