"""Denetim kaydı — docs/09 §6. `audit.read` (Yönetici, Denetçi) görür; kayıtlar değiştirilemez.

Kayıtlar otomatik yazılır (`models/audit.py`); bu uç yalnız okur. Sayfalı, yeni üstte.
Telefon, e-posta, IBAN ve vergi no kayıtta maskelidir; açan bir yol yoktur (PLT-06).

Kişisel veri görmeyen okuyucu (`people.read` yok — Denetçi, KMK m.41, docs/05): "Bugün" ve
listelerdeki ad gizleme kuralı burada da uygulanır — kişi aktörün adı, IP ve tarayıcı bilgisi
ile ayrıntıdaki ad/plaka alanları `***` döner. Sistem aktörünün adı (gece işi) açık kalır.
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
from site_yonetim.domain.masking import mask_all, mask_person_names
from site_yonetim.models import AuditLog
from site_yonetim.models.audit import ActorType, AuditAction

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
    ip: str | None = Field(description="people.read yoksa `***`")
    actor_type: ActorType = Field(
        description="user (kişi) · system (gece işi, demo) · delegate · support_session"
    )
    request_id: str | None = Field(description="isteğin kimliği (yanıttaki X-Request-ID)")
    user_agent: str | None
    reason: str | None = Field(description="hassas işlemlerde gerekçe")

    @classmethod
    def of(cls, r: AuditLog, *, personal: bool) -> AuditOut:
        """`personal`: okuyucu kişisel veriyi görebilir (`people.read`)."""

        def hide(value: str | None) -> str | None:
            return value if personal or not value else mask_all(value)

        person = r.actor_type != ActorType.SYSTEM.value
        return cls(
            id=r.id,
            at=r.created_at,
            user_id=r.user_id,
            actor_name=hide(r.actor_name) if person else r.actor_name,
            action=AuditAction(r.action),
            entity=r.entity,
            entity_id=r.entity_id,
            before=r.before if personal else mask_person_names(r.before),
            after=r.after if personal else mask_person_names(r.after),
            ip=hide(r.ip),
            actor_type=ActorType(r.actor_type),
            request_id=r.request_id,
            user_agent=hide(r.user_agent),
            reason=r.reason,
        )


@router.get("/audit", summary="Denetim kaydı (yeni üstte)")
async def list_audit(
    ctx: AuditRead,
    paging: Paging,
    entity: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    user_id: uuid.UUID | None = None,
    actor_type: ActorType | None = None,
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
    if actor_type:
        query = query.where(AuditLog.actor_type == actor_type.value)
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
    personal = ctx.access.can(Permission.PEOPLE_READ)
    return Page(
        items=[AuditOut.of(r, personal=personal) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )
