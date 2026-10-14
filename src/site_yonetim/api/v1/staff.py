"""Site personeli — frontend servis isteği 18 (backend #46).

Modül `staff` (kapalıysa 404). Okuma `people.read` (telefonu da görür; Güvenlik rolünde yok),
yazma `staff.manage` (Yönetici). Personel kaydı hesap açmaz.
"""

import dataclasses
import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
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

    @classmethod
    def of(cls, item: StaffMember, today: dt.date) -> StaffOut:
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
        )


@router.get("", summary=f"Personel (ada göre; dizi, en çok {LIST_MAX})")
async def list_staff(
    ctx: Reader,
    today: TodayDep,
    active: Annotated[
        bool | None, Query(description="true → çalışan, false → ayrılan, boş → hepsi")
    ] = None,
) -> list[StaffOut]:
    rows = await ctx.session.scalars(svc.list_query(active, today).limit(LIST_MAX))
    return [StaffOut.of(s, today) for s in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Personel ekle")
async def create_staff(body: StaffIn, ctx: Manager, today: TodayDep) -> Written[StaffOut]:
    fields = {k: getattr(body, k) for k in StaffIn.model_fields}
    try:
        item = await svc.create(ctx.session, svc.StaffData(**fields))
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = StaffOut.of(item, today)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.full_name} eklendi.")


@router.patch("/{staff_id}", summary="Personeli düzenle; `end_date` ile ayrılış (kısmi)")
async def update_staff(
    staff_id: uuid.UUID, body: StaffIn, ctx: Manager, today: TodayDep
) -> Written[StaffOut]:
    item = await svc.get(ctx.session, staff_id)
    if item is None:
        raise NotFoundError("Personel bulunamadı.")
    had_left = item.end_date is not None
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        await svc.update(ctx.session, item, dataclasses.replace(svc.current(item), **changes))
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = StaffOut.of(item, today)
    await ctx.session.commit()
    if item.end_date is not None and not had_left:
        message = f"{item.full_name} için ayrılış kaydedildi."
    else:
        message = f"{item.full_name} güncellendi."
    return Written(data=out, message=message)
