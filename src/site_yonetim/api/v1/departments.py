"""Talep departmanları — frontend servis isteği 11 (backend issue #39).

Modül `requests`; liste `requests.read`, ekle/düzenle `requests.assign`. Talebe departman atama
ucu talep router'ında: `POST /sites/{slug}/requests/{id}/department`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContext, require_module
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.requests import rule_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import RequestDepartment
from site_yonetim.services import departments as svc

router = APIRouter(prefix="/sites/{slug}/departments", tags=["talep"])
RequestsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]


def _require(ctx: SiteContext, permission: Permission) -> None:
    if not ctx.access.can(permission):
        raise ForbiddenError


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    request_count: int = Field(description="bu departmana atanmış talepler")


def _out(department: RequestDepartment, count: int) -> DepartmentOut:
    return DepartmentOut(
        id=department.id, name=department.name, is_active=department.is_active, request_count=count
    )


async def _count(ctx: SiteContext, department_id: uuid.UUID) -> int:
    return next((c for d, c in await svc.listing(ctx.session) if d.id == department_id), 0)


@router.get("", summary="Departmanlar")
async def list_departments(ctx: RequestsModule) -> list[DepartmentOut]:
    _require(ctx, Permission.REQUESTS_READ)
    return [_out(d, c) for d, c in await svc.listing(ctx.session)]


class DepartmentIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class DepartmentPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


@router.post("", status_code=status.HTTP_201_CREATED, summary="Departman ekle")
async def create_department(body: DepartmentIn, ctx: RequestsModule) -> Written[DepartmentOut]:
    _require(ctx, Permission.REQUESTS_ASSIGN)
    try:
        department = await svc.create(ctx.session, body.name)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(data=_out(department, 0), message=f'"{department.name}" departmanı eklendi.')


@router.patch("/{department_id}", summary="Yeniden adlandır ya da pasifleştir")
async def update_department(
    department_id: uuid.UUID, body: DepartmentPatch, ctx: RequestsModule
) -> Written[DepartmentOut]:
    _require(ctx, Permission.REQUESTS_ASSIGN)
    department = await svc.get(ctx.session, department_id)
    if department is None:
        raise NotFoundError("Departman bulunamadı.")
    try:
        await svc.update(ctx.session, department, name=body.name, is_active=body.is_active)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    count = await _count(ctx, department.id)
    await ctx.session.commit()
    if body.is_active is False:
        message = f'"{department.name}" pasifleştirildi; yeni talep atanamaz.'
    else:
        message = f'"{department.name}" departmanı kaydedildi.'
    return Written(data=_out(department, count), message=message)
