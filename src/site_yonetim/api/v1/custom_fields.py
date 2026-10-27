"""Özel alanlar — servis isteği 24 (YAP-10): tanım (`modules.manage`) ve değerler.

Görünürlük rol bazlı: alanın `view_roles` listesinde olmayan rol alanı da değerini de yanıtta
görmez; `edit_roles` dışındaki rol değeri değiştiremez (403). Sakin tarafı (`resident_can_*`)
yalnız saklanır — sakin ekranı sonraki adım.
"""

import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import SiteContext, SiteContextDep, require_permission
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.structure import _load_unit
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.custom_fields import CustomEntity, CustomFieldType, KvkkCategory
from site_yonetim.models import CustomField, Person
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import custom_fields as svc

router = APIRouter(prefix="/sites/{slug}", tags=["özel alanlar"])
ModulesManage = Annotated[SiteContext, Depends(require_permission(Permission.MODULES_MANAGE))]

READ = {CustomEntity.UNIT: Permission.UNITS_READ, CustomEntity.PERSON: Permission.PEOPLE_READ}


def _error(exc: svc.CustomFieldError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.fields)


class CustomFieldOut(BaseModel):
    id: uuid.UUID
    entity: CustomEntity
    label: str
    key: str = Field(description="etiketten üretilir, değişmez (Excel ve API)")
    type: CustomFieldType
    options: list[str]
    required: bool
    kvkk_category: KvkkCategory
    view_roles: list[str]
    edit_roles: list[str]
    resident_can_view: bool
    resident_can_edit: bool
    sort_order: int
    is_archived: bool

    @classmethod
    def of(cls, f: CustomField) -> CustomFieldOut:
        return cls(
            id=f.id,
            entity=CustomEntity(f.entity),
            label=f.label,
            key=f.key,
            type=CustomFieldType(f.type),
            options=list(f.options or []),
            required=f.required,
            kvkk_category=KvkkCategory(f.kvkk_category),
            view_roles=list(f.view_roles or []),
            edit_roles=list(f.edit_roles or []),
            resident_can_view=f.resident_can_view,
            resident_can_edit=f.resident_can_edit,
            sort_order=f.sort_order,
            is_archived=f.is_archived,
        )


@router.get("/custom-fields", summary="Özel alan tanımları")
async def list_fields(
    ctx: SiteContextDep,
    entity: CustomEntity,
    archived: Annotated[bool, Query(description="arşivdekiler")] = False,
) -> list[CustomFieldOut]:
    """Tanım yetkilisi (`modules.manage`) hepsini görür; diğer roller yalnız `view_roles`
    listesinde oldukları etkin alanları (kart formları için)."""
    manager = ctx.access.can(Permission.MODULES_MANAGE)
    if not manager and not ctx.access.can(READ[entity]):
        raise ForbiddenError
    fields = await svc.fields_for(ctx.session, entity, archived=archived)
    if not manager:
        mine = set(ctx.access.role_names)  # P18: rollerin birleşimi
        fields = [f for f in fields if not f.is_archived and mine & set(f.view_roles)]
    return [CustomFieldOut.of(f) for f in fields]


class CustomFieldIn(BaseModel):
    entity: CustomEntity | None = None
    label: str | None = Field(default=None, max_length=100)
    type: CustomFieldType | None = None
    options: list[str] | None = Field(default=None, max_length=50)
    required: bool | None = None
    kvkk_category: KvkkCategory | None = None
    view_roles: list[str] | None = Field(default=None, max_length=10)
    edit_roles: list[str] | None = Field(default=None, max_length=10)
    resident_can_view: bool | None = None
    resident_can_edit: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    acknowledge_sensitive: bool = Field(
        default=False, description="özel nitelikli veri uyarısını gerekçeyle geç"
    )
    sensitive_reason: str | None = Field(default=None, max_length=300)


def _data(body: CustomFieldIn) -> svc.FieldData:
    raw = body.model_dump(exclude_unset=True)
    for key in ("entity", "type", "kvkk_category"):
        if raw.get(key) is not None:
            raw[key] = str(raw[key].value)
    return svc.FieldData(**raw)


async def _save(
    ctx: SiteContext, body: CustomFieldIn, field: CustomField | None
) -> tuple[CustomField, bool]:
    try:
        saved, sensitive = await svc.save(ctx.session, _data(body), field)
    except svc.CustomFieldError as exc:
        raise _error(exc) from None
    if sensitive:
        record(
            ctx.session, AuditAction.UPDATE, "custom_fields", saved.id,
            {"label": saved.label, "sensitive_acknowledged": True},
            reason=saved.sensitive_reason,
        )  # fmt: skip
    return saved, sensitive


@router.post("/custom-fields", status_code=status.HTTP_201_CREATED, summary="Özel alan ekle")
async def create_field(ctx: ModulesManage, body: CustomFieldIn) -> Written[CustomFieldOut]:
    field, _ = await _save(ctx, body, None)
    out = CustomFieldOut.of(field)
    await ctx.session.commit()
    return Written(data=out, message=f'"{out.label}" alanı eklendi; formlarda görünür.')


async def _field(ctx: SiteContext, field_id: uuid.UUID) -> CustomField:
    found = await ctx.session.scalar(select(CustomField).where(CustomField.id == field_id))
    if found is None:
        raise NotFoundError("Özel alan bulunamadı.")
    return found


@router.patch("/custom-fields/{field_id}", summary="Özel alan düzenle (tip değişmez)")
async def update_field(
    ctx: ModulesManage, field_id: uuid.UUID, body: CustomFieldIn
) -> Written[CustomFieldOut]:
    field = await _field(ctx, field_id)
    if body.entity is not None and body.entity.value != field.entity:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "Formda düzeltilmesi gereken alanlar var.",
            {"entity": "Alanın nesnesi değiştirilemez."},
        )
    await _save(ctx, body, field)
    out = CustomFieldOut.of(field)
    await ctx.session.commit()
    return Written(data=out, message=f'"{out.label}" güncellendi.')


@router.post("/custom-fields/{field_id}/archive", summary="Arşivle (silme yok)")
async def archive_field(ctx: ModulesManage, field_id: uuid.UUID) -> Written[CustomFieldOut]:
    field = await _field(ctx, field_id)
    field.is_archived = True
    out = CustomFieldOut.of(field)
    await ctx.session.commit()
    return Written(data=out, message=f'"{out.label}" arşivlendi; verisi saklama süresince korunur.')


# --- Değerler ---------------------------------------------------------------------------------


class CustomValuesOut(BaseModel):
    values: dict[str, Any] = Field(description="`{anahtar: değer}` — yalnız görülebilen alanlar")


class CustomValuesIn(BaseModel):
    values: dict[str, Any] = Field(description="yalnız gönderilen anahtarlar değişir; boş siler")


async def _visible(ctx: SiteContext, entity: CustomEntity) -> list[CustomField]:
    if not ctx.access.can(READ[entity]):
        raise ForbiddenError
    mine = set(ctx.access.role_names)  # P18: rollerin birleşimi
    return [
        f
        for f in await svc.fields_for(ctx.session, entity, archived=False)
        if mine & set(f.view_roles)
    ]


async def _require_entity(ctx: SiteContext, entity: CustomEntity, entity_id: uuid.UUID) -> None:
    if entity is CustomEntity.UNIT:
        await _load_unit(ctx, entity_id)
        return
    found = await ctx.session.scalar(
        select(Person.id).where(Person.id == entity_id, Person.merged_into_id.is_(None))
    )
    if found is None:
        raise NotFoundError("Kişi bulunamadı.")


async def _get_values(
    ctx: SiteContext, entity: CustomEntity, entity_id: uuid.UUID
) -> CustomValuesOut:
    fields = await _visible(ctx, entity)
    await _require_entity(ctx, entity, entity_id)
    return CustomValuesOut(values=await svc.values_of(ctx.session, entity_id, fields))


async def _put_values(
    ctx: SiteContext, entity: CustomEntity, entity_id: uuid.UUID, body: CustomValuesIn
) -> Written[CustomValuesOut]:
    fields = await _visible(ctx, entity)
    await _require_entity(ctx, entity, entity_id)
    mine = set(ctx.access.role_names)
    editable = {f.key for f in fields if mine & set(f.edit_roles)}
    try:
        values = await svc.write_values(
            ctx.session, entity_id, fields, body.values, editable=editable
        )
    except svc.CustomFieldError as exc:
        raise _error(exc) from None
    await ctx.session.commit()
    return Written(data=CustomValuesOut(values=values), message="Özel alanlar kaydedildi.")


@router.get("/units/{unit_id}/custom-values", summary="Bölümün özel alan değerleri")
async def unit_values(ctx: SiteContextDep, unit_id: uuid.UUID) -> CustomValuesOut:
    return await _get_values(ctx, CustomEntity.UNIT, unit_id)


@router.put("/units/{unit_id}/custom-values", summary="Bölümün özel alan değerlerini kaydet")
async def put_unit_values(
    ctx: SiteContextDep, unit_id: uuid.UUID, body: CustomValuesIn
) -> Written[CustomValuesOut]:
    return await _put_values(ctx, CustomEntity.UNIT, unit_id, body)


@router.get("/people/{person_id}/custom-values", summary="Kişinin özel alan değerleri")
async def person_values(ctx: SiteContextDep, person_id: uuid.UUID) -> CustomValuesOut:
    return await _get_values(ctx, CustomEntity.PERSON, person_id)


@router.put("/people/{person_id}/custom-values", summary="Kişinin özel alan değerlerini kaydet")
async def put_person_values(
    ctx: SiteContextDep, person_id: uuid.UUID, body: CustomValuesIn
) -> Written[CustomValuesOut]:
    return await _put_values(ctx, CustomEntity.PERSON, person_id, body)
