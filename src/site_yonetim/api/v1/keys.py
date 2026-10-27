"""Anahtar, kart ve emanet anahtar — Yazılım Dokümanı OPS-01 (servis isteği 53).

Çekirdek (modül yok). Personel uçları `requests.assign` **ya da** `security.visitors` (güvenlik
masası); sakin uçları (`/resident/deposited-keys`) oturumdaki sakinin bugünkü bölümleri.
Listeler küçük ve dizidir (anahtar listesi tam; teslim geçmişi son 100).
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.ops_shared import any_permission, fields_error, ops_error
from site_yonetim.api.v1.resident import resident
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import DepositStatus, KeyKind, OpsError, deposit_status
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import DepositedKey, FacilityKey, KeyLoan
from site_yonetim.services import keys as svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}", tags=["anahtar ve emanet"])
Desk = Annotated[
    SiteContext,
    Depends(any_permission(Permission.REQUESTS_ASSIGN, Permission.SECURITY_VISITORS)),
]
Resident = Annotated[SiteContext, Depends(resident)]
LOANS_MAX = 100


class KeyIn(BaseModel):
    kind: str | None = Field(default=None, max_length=10, description="key · card · remote")
    code: str | None = Field(default=None, max_length=60)
    name: str | None = Field(default=None, max_length=200)
    area: str | None = Field(default=None, max_length=200)
    authorized_roles: list[str] | None = Field(
        default=None, max_length=10, description="boş: herkes; ör. Yönetici, Görevli"
    )
    is_active: bool | None = None


class KeyLoanOut(BaseModel):
    id: uuid.UUID
    key_id: uuid.UUID
    key_name: str | None
    to_name: str
    to_phone: str | None
    role: str | None
    due_at: dt.datetime
    given_by: str
    given_at: dt.datetime
    returned_at: dt.datetime | None
    returned_to: str | None

    @classmethod
    def of(cls, loan: KeyLoan, key: FacilityKey | None = None) -> KeyLoanOut:
        return cls(
            id=loan.id,
            key_id=loan.key_id,
            key_name=key.name if key else None,
            to_name=loan.to_name,
            to_phone=loan.to_phone,
            role=loan.role,
            due_at=loan.due_at,
            given_by=loan.given_by_name,
            given_at=loan.created_at,
            returned_at=loan.returned_at,
            returned_to=loan.returned_to_name,
        )


class FacilityKeyOut(BaseModel):
    id: uuid.UUID
    kind: KeyKind
    code: str
    name: str
    area: str | None
    authorized_roles: list[str]
    is_active: bool
    loan: KeyLoanOut | None = Field(description="açık teslim (anahtar dışarıda)")
    overdue: bool = Field(description="açık teslimin iade zamanı geçti")

    @classmethod
    def of(cls, key: FacilityKey, loan: KeyLoan | None, now: dt.datetime) -> FacilityKeyOut:
        return cls(
            id=key.id,
            kind=KeyKind(key.kind),
            code=key.code,
            name=key.name,
            area=key.area,
            authorized_roles=list(key.authorized_roles),
            is_active=key.is_active,
            loan=KeyLoanOut.of(loan, key) if loan else None,
            overdue=loan is not None and loan.due_at < now,
        )


@router.get("/keys", summary="Anahtar, kart ve kumandalar (koda göre) + açık teslim")
async def list_keys(ctx: Desk, now: NowDep) -> list[FacilityKeyOut]:
    return [FacilityKeyOut.of(k, loan, now) for k, loan in await svc.keys_with_loans(ctx.session)]


def _key_data(body: KeyIn, current: FacilityKey | None) -> svc.KeyData:
    sent = body.model_fields_set

    def pick[T](name: str, value: T, old: T) -> T:
        return value if name in sent or current is None else old

    return svc.KeyData(
        kind=pick("kind", body.kind, current.kind if current else None),
        code=pick("code", body.code, current.code if current else None),
        name=pick("name", body.name, current.name if current else None),
        area=pick("area", body.area, current.area if current else None),
        authorized_roles=pick(
            "authorized_roles",
            body.authorized_roles,
            list(current.authorized_roles) if current else None,
        ),
        is_active=pick("is_active", body.is_active, current.is_active if current else None),
    )


@router.post("/keys", status_code=status.HTTP_201_CREATED, summary="Anahtar tanımı ekle")
async def create_key(body: KeyIn, ctx: Desk, now: NowDep) -> Written[FacilityKeyOut]:
    try:
        key = await svc.save_key(ctx.session, _key_data(body, None), None)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = FacilityKeyOut.of(key, None, now)
    await ctx.session.commit()
    return Written(data=out, message=f"{key.name} eklendi.")


@router.patch("/keys/{key_id}", summary="Anahtar tanımını düzenle (kısmi)")
async def update_key(
    key_id: uuid.UUID, body: KeyIn, ctx: Desk, now: NowDep
) -> Written[FacilityKeyOut]:
    key = await svc.get_key(ctx.session, key_id, lock=True)
    if key is None:
        raise NotFoundError("Anahtar bulunamadı.")
    try:
        await svc.save_key(ctx.session, _key_data(body, key), key)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    loan = await svc.open_loan(ctx.session, key.id)
    out = FacilityKeyOut.of(key, loan, now)
    await ctx.session.commit()
    return Written(data=out, message=f"{key.name} güncellendi.")


class LoanIn(BaseModel):
    to_name: str | None = Field(default=None, max_length=200)
    to_phone: str | None = Field(default=None, max_length=30)
    role: str | None = Field(default=None, max_length=60, description="yetkili rol kontrolü")
    due_at: dt.datetime | None = Field(default=None, description="iade zamanı (ileri)")


@router.post(
    "/keys/{key_id}/loans",
    status_code=status.HTTP_201_CREATED,
    summary="Anahtarı teslim et",
    responses={409: {"description": "`key_out` · `not_authorized` · `key_inactive`"}},
)
async def give_key(
    key_id: uuid.UUID, body: LoanIn, ctx: Desk, current: CurrentUserDep, now: NowDep
) -> Written[KeyLoanOut]:
    key = await svc.get_key(ctx.session, key_id, lock=True)
    if key is None:
        raise NotFoundError("Anahtar bulunamadı.")
    try:
        loan = await svc.give(
            ctx.session,
            key,
            to_name=body.to_name,
            to_phone=body.to_phone,
            role=body.role,
            due_at=body.due_at,
            by=current.user.full_name,
            now=now,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    except OpsError as exc:
        raise ops_error(exc) from exc
    await ctx.session.refresh(loan, ["created_at"])
    out = KeyLoanOut.of(loan, key)
    await ctx.session.commit()
    return Written(data=out, message=f"{key.name} {loan.to_name} kişisine verildi.")


@router.post("/key-loans/{loan_id}/return", summary="İadeyi kaydet")
async def return_key(
    loan_id: uuid.UUID, ctx: Desk, current: CurrentUserDep, now: NowDep
) -> Written[KeyLoanOut]:
    loan = await svc.get_loan(ctx.session, loan_id)
    if loan is None:
        raise NotFoundError("Teslim kaydı bulunamadı.")
    try:
        await svc.return_loan(ctx.session, loan, by=current.user.full_name, now=now)
    except OpsError as exc:
        raise ops_error(exc) from exc
    key = await svc.get_key(ctx.session, loan.key_id)
    out = KeyLoanOut.of(loan, key)
    await ctx.session.commit()
    return Written(data=out, message="İade kaydedildi.")


@router.get("/key-loans", summary=f"Teslim geçmişi (yeniden eskiye, son {LOANS_MAX})")
async def list_loans(ctx: Desk) -> list[KeyLoanOut]:
    loans = list(await ctx.session.scalars(svc.loans_query().limit(LOANS_MAX)))
    keys = await svc.key_names(ctx.session, {loan.key_id for loan in loans})
    return [KeyLoanOut.of(loan, keys.get(loan.key_id)) for loan in loans]


# --- Emanet anahtar ------------------------------------------------------------------


class AuthorizedPerson(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)


class AuthorizedPersonOut(BaseModel):
    name: str
    phone: str | None


class DepositedKeyOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    deposited_by: str
    authorized: list[AuthorizedPersonOut]
    valid_until: dt.date
    note: str | None
    status: DepositStatus = Field(description="süresi geçen emanette `expired` (okumada hesap)")
    received_by: str | None
    received_at: dt.datetime | None
    handed_to: str | None
    handed_at: dt.datetime | None
    created_at: dt.datetime

    @classmethod
    def of(cls, d: DepositedKey, unit_name: str, today: dt.date) -> DepositedKeyOut:
        return cls(
            id=d.id,
            unit_id=d.unit_id,
            unit_name=unit_name,
            deposited_by=d.deposited_by_name,
            authorized=[
                AuthorizedPersonOut(name=str(p.get("name")), phone=p.get("phone"))
                for p in d.authorized
            ],
            valid_until=d.valid_until,
            note=d.note,
            status=deposit_status(d.status, d.valid_until, today),
            received_by=d.received_by_name,
            received_at=d.received_at,
            handed_to=d.handed_to,
            handed_at=d.handed_at,
            created_at=d.created_at,
        )


async def _deposit_outs(
    ctx: SiteContext, rows: list[DepositedKey], today: dt.date
) -> list[DepositedKeyOut]:
    names = await unit_names(ctx.session, {d.unit_id for d in rows})
    return [DepositedKeyOut.of(d, names.get(d.unit_id, "—"), today) for d in rows]


@router.get("/deposited-keys", summary="Emanet anahtarlar (yeniden eskiye, en çok 500)")
async def list_deposits(ctx: Desk, today: TodayDep) -> list[DepositedKeyOut]:
    rows = list(await ctx.session.scalars(svc.deposits_query().limit(500)))
    return await _deposit_outs(ctx, rows, today)


class HandIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    id_checked: bool = False


async def _deposit(ctx: SiteContext, deposit_id: uuid.UUID) -> DepositedKey:
    record = await svc.get_deposit(ctx.session, deposit_id)
    if record is None:
        raise NotFoundError("Emanet bulunamadı.")
    return record


async def _deposit_written(
    ctx: SiteContext, record: DepositedKey, today: dt.date, message: str
) -> Written[DepositedKeyOut]:
    await ctx.session.refresh(record)
    out = (await _deposit_outs(ctx, [record], today))[0]
    await ctx.session.commit()
    return Written(data=out, message=message)


@router.post("/deposited-keys/{deposit_id}/receive", summary="Emanet anahtarı teslim al")
async def receive_deposit(
    deposit_id: uuid.UUID, ctx: Desk, current: CurrentUserDep, today: TodayDep, now: NowDep
) -> Written[DepositedKeyOut]:
    record = await _deposit(ctx, deposit_id)
    try:
        await svc.receive(ctx.session, record, by=current.user.full_name, now=now)
    except OpsError as exc:
        raise ops_error(exc) from exc
    return await _deposit_written(
        ctx, record, today, "Emanet anahtar teslim alındı ve kayda bağlandı."
    )


@router.post(
    "/deposited-keys/{deposit_id}/hand",
    summary="Emanet anahtarı yetkili kişiye ver (kimlik kontrolüyle)",
    responses={409: {"description": "`not_authorized` · `expired` · `invalid_status`"}},
)
async def hand_deposit(
    deposit_id: uuid.UUID,
    body: HandIn,
    ctx: Desk,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[DepositedKeyOut]:
    record = await _deposit(ctx, deposit_id)
    try:
        name = await svc.hand(
            ctx.session,
            record,
            name=body.name,
            id_checked=body.id_checked,
            by=current.user.full_name,
            today=today,
            now=now,
        )
    except svc.NotAuthorizedPersonError as exc:
        await ctx.session.commit()  # sakine giden "verilmedi" bilgisi kalır
        raise ops_error(exc) from exc
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    except OpsError as exc:
        raise ops_error(exc) from exc
    return await _deposit_written(
        ctx, record, today, f"Anahtar {name} kişisine verildi; sakine bildirim gitti."
    )


@router.post("/deposited-keys/{deposit_id}/return", summary="Anahtarı sahibine iade et")
async def return_deposit(
    deposit_id: uuid.UUID, ctx: Desk, today: TodayDep, now: NowDep
) -> Written[DepositedKeyOut]:
    record = await _deposit(ctx, deposit_id)
    try:
        await svc.give_back(ctx.session, record, now=now)
    except OpsError as exc:
        raise ops_error(exc) from exc
    return await _deposit_written(ctx, record, today, "Anahtar sahibine iade edildi.")


# --- Sakin --------------------------------------------------------------------------


class ResidentDepositIn(BaseModel):
    authorized: list[AuthorizedPerson] = Field(default_factory=list, max_length=20)
    valid_until: dt.date | None = None
    note: str | None = Field(default=None, max_length=1000)
    unit_id: uuid.UUID | None = Field(default=None, description="çok bölümlü sakin; boş: ilki")


async def _my_units(ctx: SiteContext, today: dt.date) -> list[uuid.UUID]:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı engeller
        return []
    return sorted(await requests_svc.person_unit_ids(ctx.session, person, today))


@router.get("/resident/deposited-keys", summary="Sakin — bölümlerimin emanet anahtarları")
async def my_deposits(ctx: Resident, today: TodayDep) -> list[DepositedKeyOut]:
    units = await _my_units(ctx, today)
    if not units:
        return []
    rows = list(
        await ctx.session.scalars(svc.deposits_query().where(DepositedKey.unit_id.in_(units)))
    )
    return await _deposit_outs(ctx, rows, today)


@router.post(
    "/resident/deposited-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Sakin — emanet anahtar kaydı aç (yetkili kişiler, geçerlilik)",
)
async def create_my_deposit(
    body: ResidentDepositIn, ctx: Resident, current: CurrentUserDep, today: TodayDep
) -> Written[DepositedKeyOut]:
    units = await _my_units(ctx, today)
    unit = body.unit_id if body.unit_id is not None else (units[0] if units else None)
    if unit is None or unit not in units:
        raise NotFoundError("Bölümünüz bulunamadı.")
    try:
        record = await svc.deposit(
            ctx.session,
            unit_id=unit,
            person_id=ctx.access.person_id,
            user_id=current.user.id,
            by=current.user.full_name,
            authorized=[svc.Authorized(a.name, a.phone) for a in body.authorized],
            valid_until=body.valid_until,
            note=body.note,
            today=today,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    return await _deposit_written(
        ctx,
        record,
        today,
        "Emanet kaydı oluşturuldu; anahtarı yönetime/güvenliğe bıraktığınızda teslim alınacak.",
    )
