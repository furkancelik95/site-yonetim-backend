"""Denetim kaydı — docs/09 §6. `audit.read` (Yönetici, Denetçi) görür; kayıtlar değiştirilemez.

Kayıtlar otomatik yazılır (`models/audit.py`); bu uç yalnız okur. Sayfalı, yeni üstte.
"""

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import BUSINESS_TZ, SiteContext, require_permission
from site_yonetim.api.schemas import Page, PageParams
from site_yonetim.domain.access import Permission
from site_yonetim.models import AuditLog
from site_yonetim.models.audit import AuditAction

router = APIRouter(prefix="/sites/{slug}", tags=["denetim"])
AuditRead = Annotated[SiteContext, Depends(require_permission(Permission.AUDIT_READ))]
Paging = Annotated[PageParams, Depends()]


class AuditOut(BaseModel):
    id: uuid.UUID
    at: dt.datetime
    user_id: uuid.UUID | None = Field(description="null: sistem (demo, gece işi)")
    actor_name: str | None
    action: AuditAction
    entity: str = Field(description="tablo adı: `payments`, `expenses`, `site_modules` …")
    entity_id: uuid.UUID | None
    before: dict[str, Any] | None = Field(description="değişiklikte eski değerler")
    after: dict[str, Any] | None = Field(description="yeni değerler / olay ayrıntısı")
    ip: str | None


@router.get("/audit", summary="Denetim kaydı (yeni üstte)")
async def list_audit(
    ctx: AuditRead,
    paging: Paging,
    entity: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    user_id: uuid.UUID | None = None,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> Page[AuditOut]:
    query = select(AuditLog)
    if entity:
        query = query.where(AuditLog.entity == entity)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if action:
        query = query.where(AuditLog.action == action.value)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if date_from:
        start = dt.datetime.combine(date_from, dt.time.min, BUSINESS_TZ)
        query = query.where(AuditLog.created_at >= start)
    if date_to:
        end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, BUSINESS_TZ)
        query = query.where(AuditLog.created_at < end)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset(paging.offset)
        .limit(paging.page_size)
    )
    return Page(
        items=[
            AuditOut(
                id=r.id,
                at=r.created_at,
                user_id=r.user_id,
                actor_name=r.actor_name,
                action=AuditAction(r.action),
                entity=r.entity,
                entity_id=r.entity_id,
                before=r.before,
                after=r.after,
                ip=r.ip,
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )
