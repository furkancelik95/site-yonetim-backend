"""Güvenlik olay kaydı ve kayıp eşya — frontend servis istekleri 09, 10 (backend #37, #38).

Modül `visitors` (kapalıysa 404); izin `security.incidents` (Yönetici, Güvenlik). Denetçi
erişmez: olay açıklamasında kişisel veri olabilir (docs/05). Kayıtlar denetim kaydında.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    require_module,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import IncidentKind, IncidentStatus, LostItemStatus
from site_yonetim.models import Incident, LostItem
from site_yonetim.services import incidents as svc
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])
Paging = Annotated[PageParams, Depends()]


async def _incidents(
    ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.VISITORS))],
) -> SiteContext:
    if not ctx.access.can(Permission.SECURITY_INCIDENTS):
        raise ForbiddenError
    return ctx


Incidents = Annotated[SiteContext, Depends(_incidents)]


def _error(exc: OperationRuleError) -> ApiError:
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


# --- Olay ---------------------------------------------------------------------------


class IncidentIn(BaseModel):
    kind: str | None = Field(default=None, max_length=30)
    location: str | None = Field(default=None, max_length=400)
    description: str | None = Field(default=None, max_length=4000)
    occurred_at: dt.datetime | None = Field(default=None, description="boşsa şimdi")
    unit_id: uuid.UUID | None = None


class CloseIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class IncidentOut(BaseModel):
    id: uuid.UUID
    number: int
    occurred_at: dt.datetime
    kind: IncidentKind
    location: str
    description: str
    unit_id: uuid.UUID | None
    unit_name: str | None
    status: IncidentStatus
    closed_note: str | None
    closed_at: dt.datetime | None
    recorded_by: str | None
    created_at: dt.datetime


async def _incident_out(ctx: SiteContext, rows: list[Incident]) -> list[IncidentOut]:
    names = await unit_names(ctx.session, {r.unit_id for r in rows if r.unit_id})
    return [
        IncidentOut(
            id=r.id,
            number=r.number,
            occurred_at=r.occurred_at,
            kind=IncidentKind(r.kind),
            location=r.location,
            description=r.description,
            unit_id=r.unit_id,
            unit_name=names.get(r.unit_id) if r.unit_id else None,
            status=IncidentStatus(r.status),
            closed_note=r.closed_note,
            closed_at=r.closed_at,
            recorded_by=r.recorded_by,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/incidents", summary="Olaylar (yeni üstte)")
async def list_incidents(
    ctx: Incidents,
    paging: Paging,
    status_: Annotated[IncidentStatus | None, Query(alias="status")] = None,
) -> Page[IncidentOut]:
    query = svc.incidents_query(status_)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _incident_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post("/incidents", status_code=status.HTTP_201_CREATED, summary="Olay kaydet")
async def create_incident(
    body: IncidentIn, ctx: Incidents, current: CurrentUserDep, now: NowDep
) -> Written[IncidentOut]:
    try:
        incident = await svc.create_incident(
            ctx.session,
            svc.NewIncident(
                body.kind, body.location, body.description, body.occurred_at, body.unit_id
            ),
            now=now,
            recorded_by=current.user.full_name,
        )
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.refresh(incident, ["created_at"])
    [out] = await _incident_out(ctx, [incident])
    await ctx.session.commit()
    return Written(data=out, message=f"#{incident.number} numaralı olay kaydedildi.")


@router.post("/incidents/{incident_id}/close", summary="Olayı kapat (not zorunlu)")
async def close_incident(
    incident_id: uuid.UUID, body: CloseIn, ctx: Incidents, current: CurrentUserDep, now: NowDep
) -> Written[IncidentOut]:
    incident = await svc.get_incident(ctx.session, incident_id, lock=True)
    if incident is None:
        raise NotFoundError("Olay bulunamadı.")
    try:
        await svc.close_incident(
            ctx.session, incident, note=body.note, closed_by=current.user.full_name, now=now
        )
    except OperationRuleError as exc:
        raise _error(exc) from exc
    [out] = await _incident_out(ctx, [incident])
    await ctx.session.commit()
    return Written(data=out, message=f"#{incident.number} numaralı olay kapatıldı.")


# --- Kayıp eşya ---------------------------------------------------------------------


class LostItemIn(BaseModel):
    description: str | None = Field(default=None, max_length=400)
    location: str | None = Field(default=None, max_length=400)
    found_by: str | None = Field(default=None, max_length=200)
    found_at: dt.datetime | None = Field(default=None, description="boşsa şimdi")


class SettleIn(BaseModel):
    returned_to: str | None = Field(default=None, max_length=200)
    disposed: bool = Field(default=False, description="bağış/imha; `returned_to` gerekmez")


class LostItemOut(BaseModel):
    id: uuid.UUID
    number: int
    found_at: dt.datetime
    description: str
    location: str
    found_by: str | None
    status: LostItemStatus
    returned_to: str | None
    returned_at: dt.datetime | None
    recorded_by: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, item: LostItem) -> LostItemOut:
        return cls(
            id=item.id,
            number=item.number,
            found_at=item.found_at,
            description=item.description,
            location=item.location,
            found_by=item.found_by,
            status=LostItemStatus(item.status),
            returned_to=item.returned_to,
            returned_at=item.returned_at,
            recorded_by=item.recorded_by,
            created_at=item.created_at,
        )


@router.get("/lost-items", summary="Kayıp eşya (yeni üstte)")
async def list_lost_items(
    ctx: Incidents,
    paging: Paging,
    status_: Annotated[LostItemStatus | None, Query(alias="status")] = None,
) -> Page[LostItemOut]:
    query = svc.lost_items_query(status_)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size))
    return Page(
        items=[LostItemOut.of(r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post("/lost-items", status_code=status.HTTP_201_CREATED, summary="Kayıp eşya kaydet")
async def create_lost_item(
    body: LostItemIn, ctx: Incidents, current: CurrentUserDep, now: NowDep
) -> Written[LostItemOut]:
    try:
        item = await svc.create_lost_item(
            ctx.session,
            description=body.description,
            location=body.location,
            found_by=body.found_by,
            found_at=body.found_at,
            now=now,
            recorded_by=current.user.full_name,
        )
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.refresh(item, ["created_at"])
    out = LostItemOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message=f"Kayıp eşya #{item.number} kaydedildi.")


@router.post("/lost-items/{item_id}/return", summary="Teslim et ya da elden çıkar")
async def settle_lost_item(
    item_id: uuid.UUID, body: SettleIn, ctx: Incidents, now: NowDep
) -> Written[LostItemOut]:
    item = await svc.get_lost_item(ctx.session, item_id, lock=True)
    if item is None:
        raise NotFoundError("Kayıp eşya bulunamadı.")
    try:
        await svc.settle_lost_item(
            ctx.session, item, returned_to=body.returned_to, disposed=body.disposed, now=now
        )
    except OperationRuleError as exc:
        raise _error(exc) from exc
    out = LostItemOut.of(item)
    await ctx.session.commit()
    if body.disposed:
        message = f"Kayıp eşya #{item.number} elden çıkarıldı olarak işaretlendi."
    else:
        message = f"Kayıp eşya #{item.number} {item.returned_to} kişisine teslim edildi."
    return Written(data=out, message=message)
