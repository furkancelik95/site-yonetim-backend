"""Denetim (kontrol) listeleri — Yazılım Dokümanı OPS-03 (servis isteği 55). Açık site
kapsamında; transaction'ı çağıran yönetir.

- Şablon maddeleri: evet/hayır, sayı (min–maks), puan (1–5); fotoğraf zorunlu olabilir. Hazır
  şablonlar (temizlik, havuz hijyeni, güvenlik turu, taşınma hasar kontrolü, sosyal tesis açılış)
  sitenin ilk şablon listesinde oluşturulur. Şablon silinmez, pasifleşir.
- Uygunluk: evet/hayır → "evet"; sayı → aralıkta; puan → ≥ 3. Skor = uygun madde oranı.
- Olumsuz her madde için ortak alan talebi (iş emri) açılır (`open_requests`, varsayılan açık;
  talep modülü kapalıysa açılmaz) ve talep kimlikleri denetime bağlanır. Denetim değişmez.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.facility import (
    CHECKLIST_ITEMS_MAX,
    DEFAULT_CHECKLISTS,
    ChecklistItem,
    ChecklistItemType,
    answer_ok,
    clean,
    inspection_score,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import RequestCategory, RequestPriority
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import ChecklistInspection, ChecklistTemplate
from site_yonetim.models.operations import TITLE_MAX
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.facility_common import PhotoSink, save_photo


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def _num_json(value: Decimal | None) -> str | None:
    """Decimal("7.20") → "7.2" (JSON'da metin; kayan nokta yok)."""
    if value is None:
        return None
    text = f"{value.normalize():f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def items_of(raw: list[dict[str, Any]]) -> list[ChecklistItem]:
    return [
        ChecklistItem(
            text=str(i["text"]),
            type=ChecklistItemType(i["type"]),
            photo_required=bool(i.get("photo_required")),
            min=_dec(i.get("min")),
            max=_dec(i.get("max")),
        )
        for i in raw
    ]


def items_json(items: list[ChecklistItem] | tuple[ChecklistItem, ...]) -> list[dict[str, Any]]:
    return [
        {
            "text": i.text,
            "type": i.type.value,
            "photo_required": i.photo_required,
            "min": _num_json(i.min),
            "max": _num_json(i.max),
        }
        for i in items
    ]


async def ensure_defaults(session: AsyncSession) -> None:
    """Sitenin hiç şablonu yoksa hazır şablonları ekler (ilk açılışta bir kez)."""
    if await session.scalar(select(func.count()).select_from(ChecklistTemplate)):
        return
    for name, items in DEFAULT_CHECKLISTS:
        session.add(
            ChecklistTemplate(
                id=uuid.uuid7(), name=name, name_key=ascii_fold(name), items=items_json(items)
            )
        )
    await session.flush()


def templates_query() -> Select[ChecklistTemplate]:
    return select(ChecklistTemplate).order_by(ChecklistTemplate.name_key, ChecklistTemplate.id)


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> ChecklistTemplate | None:
    found: ChecklistTemplate | None = await session.scalar(
        select(ChecklistTemplate).where(ChecklistTemplate.id == template_id)
    )
    return found


@dataclass(frozen=True, slots=True)
class ItemData:
    text: str | None
    type: str | None
    photo_required: bool
    min: str | None
    max: str | None


def check_items(raw: list[ItemData]) -> list[ChecklistItem]:
    errors: dict[str, str] = {}
    items: list[ChecklistItem] = []
    for index, item in enumerate(raw):
        text = clean(item.text, 120)
        if not text:
            continue
        if item.type not in {t.value for t in ChecklistItemType}:
            errors[f"items.{index}"] = "Madde tipini seçin."
            continue
        kind = ChecklistItemType(item.type)
        low, high = _dec(item.min), _dec(item.max)
        if kind is ChecklistItemType.NUMBER and low is not None and high is not None and low > high:
            errors[f"items.{index}"] = "En az değer en çok değerden büyük olamaz."
        if kind is not ChecklistItemType.NUMBER:
            low = high = None
        items.append(ChecklistItem(text, kind, item.photo_required, low, high))
    if not items:
        errors["items"] = "En az bir madde ekleyin."
    elif len(items) > CHECKLIST_ITEMS_MAX:
        errors["items"] = f"En çok {CHECKLIST_ITEMS_MAX} madde."
    if errors:
        raise FormFieldError(errors)
    return items


async def save_template(
    session: AsyncSession,
    *,
    name: str | None,
    items: list[ItemData] | None,
    is_active: bool | None,
    current: ChecklistTemplate | None,
) -> ChecklistTemplate:
    errors: dict[str, str] = {}
    label = clean(name, 60)
    if len(label) < 2:
        errors["name"] = "Şablon adını yazın."
    elif await session.scalar(
        select(ChecklistTemplate.id).where(
            ChecklistTemplate.name_key == ascii_fold(label),
            ChecklistTemplate.id != (current.id if current else uuid.UUID(int=0)),
        )
    ):
        errors["name"] = "Bu adla bir şablon var."
    checked: list[ChecklistItem] | None = None
    if items is not None or current is None:
        try:
            checked = check_items(items or [])
        except FormFieldError as exc:
            errors.update(exc.fields)
    if errors:
        raise FormFieldError(errors)
    template = current or ChecklistTemplate(id=uuid.uuid7())
    template.name = label
    template.name_key = ascii_fold(label)
    if checked is not None:
        template.items = items_json(checked)
    if is_active is not None or current is None:
        template.is_active = True if is_active is None else is_active
    if current is None:
        session.add(template)
    await session.flush()
    return template


@dataclass(frozen=True, slots=True)
class AnswerData:
    value: str | None
    photo: str | None


@dataclass(frozen=True, slots=True)
class Inspector:
    name: str
    user_id: uuid.UUID | None


async def inspect(
    session: AsyncSession,
    sink: PhotoSink,
    *,
    template: ChecklistTemplate | None,
    location: str | None,
    answers: list[AnswerData],
    open_requests: bool,
    by: Inspector,
) -> ChecklistInspection:
    errors: dict[str, str] = {}
    if template is None or not template.is_active:
        errors["template_id"] = "Şablon seçin."
    place = clean(location, 80)
    if len(place) < 2:
        errors["location"] = "Konumu yazın."
    if template is None or errors:
        raise FormFieldError(errors)
    items = items_of(template.items)
    results: list[bool] = []
    for index, item in enumerate(items):
        answer = answers[index] if index < len(answers) else AnswerData(None, None)
        value = (answer.value or "").strip()
        ok = answer_ok(item, value) if value else None
        if not value:
            errors[f"answers.{index}"] = "Yanıtlayın."
        elif ok is None:
            errors[f"answers.{index}"] = "Geçersiz yanıt."
        elif item.photo_required and not answer.photo:
            errors[f"answers.{index}"] = "Bu madde için fotoğraf zorunlu."
        results.append(bool(ok))
    if errors:
        raise FormFieldError(errors)
    stored: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        answer = answers[index]
        photo_id = await save_photo(
            session, sink, answer.photo, by=by.name, field_name=f"answers.{index}"
        )
        stored.append(
            {
                "text": item.text,
                "type": item.type.value,
                "value": (answer.value or "").strip(),
                "ok": results[index],
                "photo_id": str(photo_id) if photo_id else None,
            }
        )
    opened: list[str] = []
    if open_requests:
        for failed in (a for a in stored if not a["ok"]):
            shown = {"yes": "Evet", "no": "Hayır"}.get(failed["value"], failed["value"])
            request = await requests_svc.create(
                session,
                requests_svc.NewRequest(
                    title=f"Denetim: {failed['text']}"[:TITLE_MAX],
                    description=(f"{template.name} denetiminde olumsuz ({place}). Değer: {shown}."),
                    category=RequestCategory.COMMON_AREA,
                    priority=RequestPriority.NORMAL,
                    unit_id=None,
                    location=place,
                    reported_by_person_id=None,
                ),
                actor=by.name,
            )
            opened.append(str(request.id))
    record = ChecklistInspection(
        id=uuid.uuid7(),
        template_id=template.id,
        template_name=template.name,
        location=place,
        answers=stored,
        score=inspection_score(results),
        failed=results.count(False),
        opened_request_ids=opened,
        by_name=by.name,
        by_user_id=by.user_id,
    )
    session.add(record)
    await session.flush()
    return record


def inspections_query() -> Select[ChecklistInspection]:
    return select(ChecklistInspection).order_by(
        ChecklistInspection.created_at.desc(), ChecklistInspection.id.desc()
    )
