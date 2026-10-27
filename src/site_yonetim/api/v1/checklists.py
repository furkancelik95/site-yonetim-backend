"""Denetim (kontrol) listeleri — Yazılım Dokümanı OPS-03 (servis isteği 55).

Çekirdek (modül yok); izin `requests.assign`. Olumsuz madde için talep yalnız talep modülü açıksa
açılır. Fotoğraf veri adresi olarak gelir, dosya deposuna yazılır; yanıtta `photo_id` döner
(`GET …/operation-photos/{id}`).
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, SiteContext, require_permission
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.expenses import StoreDep
from site_yonetim.api.v1.ops_shared import fields_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import ChecklistItemType
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import ChecklistInspection, ChecklistTemplate
from site_yonetim.services import checklists as svc
from site_yonetim.services.facility_common import PhotoSink

router = APIRouter(prefix="/sites/{slug}", tags=["denetim listeleri"])
Assigner = Annotated[SiteContext, Depends(require_permission(Permission.REQUESTS_ASSIGN))]
LIST_MAX = 200


class ChecklistItemIn(BaseModel):
    text: str | None = Field(default=None, max_length=400)
    type: str | None = Field(default="yesno", max_length=10, description="yesno · number · score")
    photo_required: bool = False
    min: Decimal | None = Field(default=None, description="sayı maddesinde alt sınır")
    max: Decimal | None = Field(default=None, description="sayı maddesinde üst sınır")


class ChecklistTemplateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    items: list[ChecklistItemIn] | None = Field(default=None, max_length=60)
    is_active: bool | None = None


class ChecklistItemOut(BaseModel):
    text: str
    type: ChecklistItemType
    photo_required: bool
    min: str | None = Field(description='ondalık metin ("7.2")')
    max: str | None


class ChecklistTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    items: list[ChecklistItemOut]
    is_active: bool

    @classmethod
    def of(cls, t: ChecklistTemplate) -> ChecklistTemplateOut:
        return cls(
            id=t.id,
            name=t.name,
            items=[ChecklistItemOut.model_validate(i) for i in t.items],
            is_active=t.is_active,
        )


@router.get("/checklist-templates", summary="Denetim şablonları (ilk açılışta hazır şablonlar)")
async def list_templates(ctx: Assigner) -> list[ChecklistTemplateOut]:
    await svc.ensure_defaults(ctx.session)
    rows = list(await ctx.session.scalars(svc.templates_query().limit(LIST_MAX)))
    await ctx.session.commit()
    return [ChecklistTemplateOut.of(t) for t in rows]


def _items(body: ChecklistTemplateIn) -> list[svc.ItemData] | None:
    if body.items is None:
        return None
    return [
        svc.ItemData(
            text=i.text,
            type=i.type,
            photo_required=i.photo_required,
            min=None if i.min is None else str(i.min),
            max=None if i.max is None else str(i.max),
        )
        for i in body.items
    ]


@router.post("/checklist-templates", status_code=status.HTTP_201_CREATED, summary="Şablon ekle")
async def create_template(
    body: ChecklistTemplateIn, ctx: Assigner
) -> Written[ChecklistTemplateOut]:
    try:
        template = await svc.save_template(
            ctx.session, name=body.name, items=_items(body), is_active=body.is_active, current=None
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = ChecklistTemplateOut.of(template)
    await ctx.session.commit()
    return Written(data=out, message="Şablon eklendi.")


@router.patch("/checklist-templates/{template_id}", summary="Şablonu düzenle / pasifleştir")
async def update_template(
    template_id: uuid.UUID, body: ChecklistTemplateIn, ctx: Assigner
) -> Written[ChecklistTemplateOut]:
    template = await svc.get_template(ctx.session, template_id)
    if template is None:
        raise NotFoundError("Şablon bulunamadı.")
    try:
        await svc.save_template(
            ctx.session,
            name=body.name if "name" in body.model_fields_set else template.name,
            items=_items(body),
            is_active=body.is_active,
            current=template,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = ChecklistTemplateOut.of(template)
    await ctx.session.commit()
    return Written(data=out, message="Şablon güncellendi.")


class InspectionAnswerIn(BaseModel):
    value: str | None = Field(default=None, max_length=40, description='"yes" · "no" · sayı · 1–5')
    photo: str | None = Field(
        default=None, max_length=3_000_000, description="veri adresi (data:image/jpeg;base64,…)"
    )


class InspectionIn(BaseModel):
    template_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=200)
    answers: list[InspectionAnswerIn] = Field(default_factory=list, max_length=60)
    open_requests: bool = Field(default=True, description="olumsuz madde için talep aç")


class InspectionAnswerOut(BaseModel):
    text: str
    type: ChecklistItemType
    value: str
    ok: bool
    photo_id: uuid.UUID | None = Field(description="GET …/operation-photos/{id}")


class ChecklistInspectionOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    template_name: str
    location: str
    answers: list[InspectionAnswerOut]
    score: int = Field(description="uygun madde oranı (0–100)")
    failed: int
    opened_request_ids: list[uuid.UUID]
    by: str
    at: dt.datetime

    @classmethod
    def of(cls, x: ChecklistInspection) -> ChecklistInspectionOut:
        answers: list[dict[str, Any]] = x.answers
        return cls(
            id=x.id,
            template_id=x.template_id,
            template_name=x.template_name,
            location=x.location,
            answers=[InspectionAnswerOut.model_validate(a) for a in answers],
            score=x.score,
            failed=x.failed,
            opened_request_ids=[uuid.UUID(i) for i in x.opened_request_ids],
            by=x.by_name,
            at=x.created_at,
        )


@router.get("/inspections", summary=f"Denetim geçmişi (yeniden eskiye, son {LIST_MAX})")
async def list_inspections(ctx: Assigner) -> list[ChecklistInspectionOut]:
    rows = await ctx.session.scalars(svc.inspections_query().limit(LIST_MAX))
    return [ChecklistInspectionOut.of(x) for x in rows]


@router.post(
    "/inspections",
    status_code=status.HTTP_201_CREATED,
    summary="Denetimi kaydet — olumsuz maddeler için talep açılır",
)
async def create_inspection(
    body: InspectionIn, ctx: Assigner, current: CurrentUserDep, store: StoreDep
) -> Written[ChecklistInspectionOut]:
    await svc.ensure_defaults(ctx.session)
    template = await svc.get_template(ctx.session, body.template_id) if body.template_id else None
    sink = PhotoSink(store)
    try:
        record = await svc.inspect(
            ctx.session,
            sink,
            template=template,
            location=body.location,
            answers=[svc.AnswerData(a.value, a.photo) for a in body.answers],
            open_requests=body.open_requests and ModuleKey.REQUESTS in ctx.modules,
            by=svc.Inspector(current.user.full_name, current.user.id),
        )
        await ctx.session.refresh(record, ["created_at"])
        out = ChecklistInspectionOut.of(record)
        await ctx.session.commit()
    except FormFieldError as exc:
        sink.discard()
        raise fields_error(exc) from exc
    except BaseException:
        sink.discard()
        raise
    opened = len(record.opened_request_ids)
    message = f"Denetim kaydedildi: skor %{record.score}."
    if opened:
        message += f" {opened} olumsuz madde için talep açıldı."
    return Written(data=out, message=message)
