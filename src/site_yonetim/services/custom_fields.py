"""Özel alanlar (servis isteği 24, YAP-10): tanım ve değerler.

- Nesne türü başına en fazla 30 etkin alan; arşivlenen sınıra sayılmaz, silme yok.
- Anahtar etiketten bir kez üretilir, tip sonradan değişmez.
- Özel nitelikli veri çağrıştıran etiket 422 `sensitive_label`; gerekçeyle (`acknowledge_sensitive`)
  geçilir ve gerekçe denetim kaydına yazılır.
- Görünürlük rol bazlı: `view_roles` dışındaki rol alanı ve değerini yanıtta görmez;
  `edit_roles` dışındaki rol değeri değiştiremez (403).
- Değer değişikliği denetim kaydına yazılır (`custom_field_values` denetlenen tablo).
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.custom_fields import (
    FIELD_LIMIT,
    LABEL_MAX,
    OPTIONS_MAX,
    OPTIONS_MIN,
    CustomEntity,
    CustomFieldType,
    FieldValueError,
    KvkkCategory,
    coerce_value,
    field_key,
    is_sensitive_label,
)
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.roles import RoleCatalog
from site_yonetim.models import CustomField, CustomFieldValue
from site_yonetim.services import roles as roles_svc

SENSITIVE_MESSAGE = (
    "Bu alan özel nitelikli kişisel veri içeriyor olabilir. KVKK gereği bu tür veri "
    "tutulmamalıdır. Etiketi değiştirin ya da gerekçeyle devam edin."
)


class CustomFieldError(Exception):
    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


@dataclass(slots=True)
class FieldData:
    entity: str | None = None
    label: str | None = None
    type: str | None = None
    options: list[str] | None = None
    required: bool | None = None
    kvkk_category: str | None = None
    view_roles: list[str] | None = None
    edit_roles: list[str] | None = None
    resident_can_view: bool | None = None
    resident_can_edit: bool | None = None
    sort_order: int | None = None
    acknowledge_sensitive: bool = False
    sensitive_reason: str | None = None


def _roles(
    value: list[str] | None, errors: dict[str, str], key: str, catalog: RoleCatalog
) -> list[str]:
    """Sitenin etkin personel rolleri (hazır şablon ya da siteye özel rol — P18), adıyla."""
    staff = {r.name for r in catalog.staff}
    roles: list[str] = []
    for name in dict.fromkeys(value or []):
        role = catalog.by_name(name)
        if role is None or role.name not in staff:
            errors[key] = "Geçersiz rol seçildi."
        else:
            roles.append(role.name)
    return list(dict.fromkeys(roles))


async def save(
    session: AsyncSession, data: FieldData, field: CustomField | None
) -> tuple[CustomField, bool]:
    """Ekler ya da günceller. (alan, özel nitelikli uyarı geçildi mi)."""
    errors: dict[str, str] = {}
    entity = data.entity if data.entity is not None else (field.entity if field else None)
    if entity not in {e.value for e in CustomEntity}:
        errors["entity"] = "Bölüm ya da kişi seçin."
    label = " ".join(
        (data.label if data.label is not None else (field.label if field else "")).split()
    )
    if not 1 <= len(label) <= LABEL_MAX:
        errors["label"] = f"Etiket 1–{LABEL_MAX} karakter."
    type_ = data.type if data.type is not None else (field.type if field else None)
    if type_ not in {t.value for t in CustomFieldType}:
        errors["type"] = "Tip seçin."
    elif field is not None and type_ != field.type:
        errors["type"] = "Tip sonradan değiştirilemez; yeni alan açın."
    options = data.options if data.options is not None else (field.options if field else [])
    options = list(dict.fromkeys(" ".join(o.split()) for o in options if o and o.strip()))
    if type_ == CustomFieldType.SELECT.value and not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        errors["options"] = f"Seçim listesi için en az {OPTIONS_MIN} seçenek yazın."
    category = data.kvkk_category or (field.kvkk_category if field else None)
    if category not in {c.value for c in KvkkCategory}:
        errors["kvkk_category"] = "KVKK kategorisi seçin."
    catalog = await roles_svc.load_catalog(session)
    view_roles = _roles(
        data.view_roles if data.view_roles is not None else (field.view_roles if field else []),
        errors,
        "view_roles",
        catalog,
    )
    if not view_roles and "view_roles" not in errors:
        errors["view_roles"] = "En az bir rol görebilmeli."
    edit_roles = _roles(
        data.edit_roles
        if data.edit_roles is not None
        else (field.edit_roles if field else ["Yönetici"]),
        errors,
        "edit_roles",
        catalog,
    )
    if errors:
        raise CustomFieldError(422, "validation", FORM_ERROR, errors)
    if entity is None or type_ is None or category is None:  # pragma: no cover — elendi
        raise CustomFieldError(422, "validation", FORM_ERROR)

    if field is None:
        active = await session.scalar(
            select(func.count())
            .select_from(CustomField)
            .where(CustomField.entity == entity, CustomField.is_archived.is_(False))
        )
        if (active or 0) >= FIELD_LIMIT:
            raise CustomFieldError(
                409, "field_limit", f"Özel alan sınırına ulaşıldı ({FIELD_LIMIT})."
            )
    label_changed = field is None or label != field.label
    sensitive = label_changed and is_sensitive_label(label)
    if sensitive and not data.acknowledge_sensitive:
        raise CustomFieldError(
            422, "sensitive_label", SENSITIVE_MESSAGE, {"label": "Özel nitelikli veri çağrışımı."}
        )
    resident_edit = (
        data.resident_can_edit
        if data.resident_can_edit is not None
        else (field.resident_can_edit if field else False)
    )
    resident_view = (
        data.resident_can_view
        if data.resident_can_view is not None
        else (field.resident_can_view if field else False)
    ) or resident_edit
    if field is None:
        key = field_key(label) or "alan"
        taken = set(
            await session.scalars(select(CustomField.key).where(CustomField.entity == entity))
        )
        base, n = key, 2
        while key in taken:
            key = f"{base[:36]}_{n}"
            n += 1
        count = await session.scalar(
            select(func.count()).select_from(CustomField).where(CustomField.entity == entity)
        )
        field = CustomField(id=uuid.uuid7(), entity=entity, key=key, type=type_)
        field.sort_order = data.sort_order if data.sort_order is not None else (count or 0) + 1
        session.add(field)
    elif data.sort_order is not None:
        field.sort_order = data.sort_order
    field.label = label
    field.options = options if type_ == CustomFieldType.SELECT.value else []
    field.required = data.required if data.required is not None else bool(field.required)
    field.kvkk_category = category
    field.view_roles = view_roles
    field.edit_roles = edit_roles
    field.resident_can_view = resident_view
    field.resident_can_edit = resident_edit
    if sensitive:
        field.sensitive_reason = (data.sensitive_reason or "Yönetici uyarıyı gerekçeyle geçti.")[
            :300
        ]
    await session.flush()
    return field, sensitive


async def fields_for(
    session: AsyncSession, entity: CustomEntity, *, archived: bool | None = False
) -> list[CustomField]:
    query = select(CustomField).where(CustomField.entity == entity.value)
    if archived is not None:
        query = query.where(CustomField.is_archived.is_(archived))
    return list(await session.scalars(query.order_by(CustomField.sort_order, CustomField.label)))


async def values_of(
    session: AsyncSession, entity_id: uuid.UUID, fields: list[CustomField]
) -> dict[str, Any]:
    if not fields:
        return {}
    by_id = {f.id: f for f in fields}
    rows = await session.execute(
        select(CustomFieldValue.field_id, CustomFieldValue.value).where(
            CustomFieldValue.entity_id == entity_id, CustomFieldValue.field_id.in_(list(by_id))
        )
    )
    return {by_id[field_id].key: value for field_id, value in rows}


async def write_values(
    session: AsyncSession,
    entity_id: uuid.UUID,
    fields: list[CustomField],
    values: dict[str, Any],
    *,
    editable: set[str],
) -> dict[str, Any]:
    """Yalnız gönderilen anahtarlar değişir; boş değer siler. Bilinmeyen anahtar yok sayılır;
    görülen ama düzenlenemeyen alan 403 (çağıran `editable` ile verir)."""
    by_key = {f.key: f for f in fields}
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        field = by_key.get(key)
        if field is None:
            continue
        if key not in editable:
            raise CustomFieldError(
                403, "forbidden", f"{field.label} alanını düzenleme yetkiniz yok."
            )
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if field.required:
                errors[key] = f"{field.label} zorunlu."
            clean[key] = None
            continue
        try:
            clean[key] = coerce_value(
                field.label, CustomFieldType(field.type), list(field.options), raw
            )
        except FieldValueError as exc:
            errors[key] = exc.message
    if errors:
        raise CustomFieldError(422, "validation", FORM_ERROR, errors)
    existing = {
        v.field_id: v
        for v in await session.scalars(
            select(CustomFieldValue).where(
                CustomFieldValue.entity_id == entity_id,
                CustomFieldValue.field_id.in_([by_key[k].id for k in clean]),
            )
        )
    } if clean else {}  # fmt: skip
    for key, value in clean.items():
        field = by_key[key]
        current = existing.get(field.id)
        if value is None:
            if current is not None:
                await session.delete(current)
        elif current is None:
            session.add(CustomFieldValue(field_id=field.id, entity_id=entity_id, value=value))
        elif current.value != value:
            current.value = value
    await session.flush()
    return await values_of(session, entity_id, fields)
