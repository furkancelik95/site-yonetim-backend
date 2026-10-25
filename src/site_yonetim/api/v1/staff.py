"""Site personeli — frontend servis istekleri 18 ve 35 (backend #46; Yazılım Dokümanı IK-01).

Modül `staff` (kapalıysa 404). Okuma `people.read` (telefonu da görür; Güvenlik rolünde yok),
yazma `staff.manage` (Yönetici). Personel kaydı hesap açmaz; **K29**: kart sitedeki bir giriş
hesabına bağlanır, ayrılınca o hesabın site üyeliği pasifleşir.
"""

import dataclasses
import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ConflictError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.management import Employer, staff_active
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import StaffMember
from site_yonetim.services import staff as svc

router = APIRouter(prefix="/sites/{slug}/staff", tags=["personel"])
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.STAFF))]
LIST_MAX = 1000


async def _reader(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.PEOPLE_READ):
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.STAFF_MANAGE):
        raise ForbiddenError
    return ctx


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]


class StaffIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    position: str | None = Field(default=None, max_length=400)
    employer: str | None = Field(default=None, max_length=20)
    contractor_name: str | None = Field(default=None, max_length=400)
    phone: str | None = Field(default=None, max_length=40)
    start_date: dt.date | None = None
    end_date: dt.date | None = Field(default=None, description="ayrılış")
    shift: str | None = Field(default=None, max_length=400)
    payroll_no: str | None = Field(
        default=None, max_length=40, description="bordro sicil no: harf, rakam, tire; ≤ 20"
    )
    user_id: uuid.UUID | None = Field(
        default=None, description="K29: bağlı giriş hesabı (`GET …/staff/linkable-users`)"
    )


class StaffOut(BaseModel):
    id: uuid.UUID
    full_name: str
    position: str
    employer: Employer
    contractor_name: str | None
    phone: str | None = Field(description="E.164 (+905XXXXXXXXX)")
    start_date: dt.date
    end_date: dt.date | None
    shift: str | None
    is_active: bool = Field(description="ayrılışı yok ya da bugün/ileride")
    payroll_no: str | None = Field(description="bordro sicil no (T.C. kimlik no tutulmaz)")
    user_id: uuid.UUID | None = Field(description="K29: bağlı giriş hesabı")
    user_name: str | None
    access_closed_at: dt.datetime | None = Field(
        description="ayrılış nedeniyle site erişiminin kapandığı an (K29)"
    )

    @classmethod
    def of(cls, item: StaffMember, today: dt.date, users: dict[uuid.UUID, str]) -> StaffOut:
        return cls(
            id=item.id,
            full_name=item.full_name,
            position=item.position,
            employer=Employer(item.employer),
            contractor_name=item.contractor_name,
            phone=item.phone,
            start_date=item.start_date,
            end_date=item.end_date,
            shift=item.shift,
            is_active=staff_active(item.end_date, today),
            payroll_no=item.payroll_no,
            user_id=item.user_id,
            user_name=users.get(item.user_id) if item.user_id else None,
            access_closed_at=item.access_closed_at,
        )


async def _out(ctx: SiteContext, item: StaffMember, today: dt.date) -> StaffOut:
    users = await svc.user_names(ctx.session, {item.user_id} if item.user_id else set())
    return StaffOut.of(item, today, users)


def _rule(exc: svc.StaffRuleError) -> ConflictError:
    return ConflictError(exc.code, exc.message)


@router.get("", summary=f"Personel (ada göre; dizi, en çok {LIST_MAX})")
async def list_staff(
    ctx: Reader,
    today: TodayDep,
    active: Annotated[
        bool | None, Query(description="true → çalışan, false → ayrılan, boş → hepsi")
    ] = None,
) -> list[StaffOut]:
    rows = list(await ctx.session.scalars(svc.list_query(active, today).limit(LIST_MAX)))
    users = await svc.user_names(ctx.session, {s.user_id for s in rows if s.user_id})
    return [StaffOut.of(s, today, users) for s in rows]


class LinkableUserOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role_name: str
    staff_id: uuid.UUID | None = Field(description="bağlı olduğu personel kartı (varsa)")


@router.get("/linkable-users", summary="Karta bağlanabilecek giriş hesapları (K29)")
async def linkable_users(ctx: Manager) -> list[LinkableUserOut]:
    return [
        LinkableUserOut(
            user_id=x.user_id,
            full_name=x.full_name,
            email=x.email,
            role_name=x.role,
            staff_id=x.staff_id,
        )
        for x in await svc.linkable_users(ctx.session)
    ]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Personel ekle")
async def create_staff(
    body: StaffIn, ctx: Manager, today: TodayDep, now: NowDep
) -> Written[StaffOut]:
    fields = {k: getattr(body, k) for k in StaffIn.model_fields}
    try:
        item = await svc.create(ctx.session, svc.StaffData(**fields), today=today, now=now)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.StaffRuleError as exc:
        raise _rule(exc) from exc
    out = await _out(ctx, item, today)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.full_name} eklendi.")


@router.patch("/{staff_id}", summary="Personeli düzenle; `end_date` ile ayrılış (kısmi)")
async def update_staff(
    staff_id: uuid.UUID, body: StaffIn, ctx: Manager, today: TodayDep, now: NowDep
) -> Written[StaffOut]:
    item = await svc.get(ctx.session, staff_id)
    if item is None:
        raise NotFoundError("Personel bulunamadı.")
    had_left = item.end_date is not None
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        await svc.update(
            ctx.session,
            item,
            dataclasses.replace(svc.current(item), **changes),
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.StaffRuleError as exc:
        raise _rule(exc) from exc
    out = await _out(ctx, item, today)
    await ctx.session.commit()
    if item.end_date is not None and not had_left:
        message = f"{item.full_name} için ayrılış kaydedildi."
        if item.user_id is not None:
            message += (
                " Site erişimi kapatıldı."
                if item.access_closed_at is not None
                else " Site erişimi ayrılış tarihinden sonra kendiliğinden kapanır."
            )
    else:
        message = f"{item.full_name} güncellendi."
    return Written(data=out, message=message)


class StaffOpenItemsOut(BaseModel):
    future_shifts: int
    first_shift: dt.date | None
    pending_leaves: int


@router.get("/{staff_id}/open-items", summary="Ayrılışta devredilecek açık işler (bilgi)")
async def open_items(
    staff_id: uuid.UUID,
    ctx: Manager,
    today: TodayDep,
    after: Annotated[
        dt.date | None, Query(description="bu günden sonraki vardiyalar; boş → bugün")
    ] = None,
) -> StaffOpenItemsOut:
    item = await ctx.session.get(StaffMember, staff_id)
    if item is None:
        raise NotFoundError("Personel bulunamadı.")
    found = await svc.open_items(ctx.session, staff_id, after or today)
    return StaffOpenItemsOut(
        future_shifts=found.future_shifts,
        first_shift=found.first_shift,
        pending_leaves=found.pending_leaves,
    )
