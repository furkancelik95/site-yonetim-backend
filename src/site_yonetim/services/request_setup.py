"""Talep ayarları — kategoriler, SLA ve çalışma takvimi, hazır yanıtlar (HIZ-03, §12.3;
servis istekleri 47–48, docs/12 D-26). Açık site kapsamında; commit çağırana ait.

- Kategoriler site tanımlıdır; site açılışında varsayılanlar kurulur (`ensure_defaults`).
  Anahtar değişmez (talep ona bağlı), ad site içinde benzersiz; silme yok, pasifleştirme var.
- Ayarlar (takvim, otomatik kapanış günü, birleştirme penceresi, düşük puan eşiği) tek satır;
  yazılmamışsa varsayılanlar geçerlidir. Değişiklik yalnız **yeni** talepleri etkiler.
"""

import uuid
from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.operations import OperationRuleError, RequestPriority
from site_yonetim.domain.service_desk import (
    AUTO_CLOSE_MAX,
    AUTO_CLOSE_MIN,
    DEFAULT_AUTO_CLOSE_DAYS,
    DEFAULT_CALENDAR,
    DEFAULT_CATEGORIES,
    DEFAULT_HOURS,
    DEFAULT_LOW_RATING,
    DEFAULT_MERGE_WINDOW_HOURS,
    LOW_RATING_MAX,
    LOW_RATING_MIN,
    MERGE_WINDOW_MAX,
    MERGE_WINDOW_MIN,
    Calendar,
    HourType,
    RuleError,
    SlaRule,
    calendar_json,
    check_hours,
    check_range,
    parse_calendar,
)
from site_yonetim.domain.text import ascii_fold, slugify
from site_yonetim.models import ReplyTemplate, RequestCategory, RequestDepartment, RequestSettings

NAME_MIN, NAME_MAX = 2, 60
TITLE_MAX, BODY_MIN, BODY_MAX = 60, 10, 1000


def name_key(name: str) -> str:
    return " ".join(ascii_fold(name).split())


def hours_of(category: RequestCategory) -> dict[RequestPriority, int]:
    return {
        RequestPriority.LOW: category.hours_low,
        RequestPriority.NORMAL: category.hours_normal,
        RequestPriority.HIGH: category.hours_high,
        RequestPriority.URGENT: category.hours_urgent,
    }


def _set_hours(category: RequestCategory, hours: dict[RequestPriority, int]) -> None:
    category.hours_low = hours[RequestPriority.LOW]
    category.hours_normal = hours[RequestPriority.NORMAL]
    category.hours_high = hours[RequestPriority.HIGH]
    category.hours_urgent = hours[RequestPriority.URGENT]


# --- Kategoriler -----------------------------------------------------------------------------


async def ensure_defaults(session: AsyncSession) -> None:
    """Yeni site: varsayılan kategoriler (HIZ-03); var olana dokunmaz."""
    existing = set(await session.scalars(select(RequestCategory.key)))
    for order, item in enumerate(DEFAULT_CATEGORIES):
        if item.key in existing:
            continue
        category = RequestCategory(
            key=item.key,
            name=item.name,
            name_key=name_key(item.name),
            hour_type=item.hour_type.value,
            default_priority=item.priority.value,
            is_active=True,
            sort_order=order,
        )
        _set_hours(category, dict(DEFAULT_HOURS))
        session.add(category)
    await session.flush()


async def categories(session: AsyncSession, *, active_only: bool = False) -> list[RequestCategory]:
    query = select(RequestCategory).order_by(RequestCategory.sort_order, RequestCategory.name)
    if active_only:
        query = query.where(RequestCategory.is_active)
    return list(await session.scalars(query))


async def active_category(session: AsyncSession, key: str) -> RequestCategory:
    category: RequestCategory | None = await session.scalar(
        select(RequestCategory).where(RequestCategory.key == key)
    )
    if category is None or not category.is_active:
        raise OperationRuleError(
            "category_not_found", "Kategori bulunamadı ya da kullanımdan kaldırıldı.",
            field="category",
        )  # fmt: skip
    return category


@dataclass(frozen=True, slots=True)
class CategoryData:
    name: str | None
    hour_type: HourType
    resolve_hours: dict[str, object]
    default_priority: RequestPriority
    department_id: uuid.UUID | None
    is_active: bool = True


async def _check_department(session: AsyncSession, department_id: uuid.UUID | None) -> None:
    if department_id is None:
        return
    active = await session.scalar(
        select(RequestDepartment.is_active).where(RequestDepartment.id == department_id)
    )
    if not active:
        raise RuleError({"department_id": "Departman bulunamadı ya da pasif."})


async def save_category(
    session: AsyncSession, data: CategoryData, *, category: RequestCategory | None
) -> RequestCategory:
    name = " ".join((data.name or "").split())
    errors: dict[str, str] = {}
    if not NAME_MIN <= len(name) <= NAME_MAX:
        errors["name"] = f"Kategori adı {NAME_MIN}–{NAME_MAX} karakter olmalı."
    try:
        hours = check_hours(data.resolve_hours, "resolve_hours")
    except RuleError as exc:
        errors.update(exc.fields)
        hours = {}
    if errors:
        raise RuleError(errors)
    await _check_department(session, data.department_id)
    key_of_name = name_key(name)
    clash = await session.scalar(
        select(RequestCategory.id).where(
            RequestCategory.name_key == key_of_name,
            RequestCategory.id != (category.id if category else uuid.UUID(int=0)),
        )
    )
    if clash is not None:
        raise RuleError({"name": "Bu adla bir kategori zaten var."})
    if category is None:
        base = slugify(name).replace("-", "_")[:40] or "kategori"
        key, n = base, 1
        while await session.scalar(select(RequestCategory.id).where(RequestCategory.key == key)):
            n += 1
            key = f"{base}_{n}"
        order = await session.scalar(select(func.coalesce(func.max(RequestCategory.sort_order), 0)))
        category = RequestCategory(key=key, sort_order=int(order or 0) + 1)
        session.add(category)
    category.name = name
    category.name_key = key_of_name
    category.hour_type = data.hour_type.value
    category.default_priority = data.default_priority.value
    category.department_id = data.department_id
    category.is_active = data.is_active
    _set_hours(category, hours)
    await session.flush()
    return category


# --- Ayarlar ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    calendar: Calendar
    auto_close_days: int
    merge_window_hours: int
    low_rating_threshold: int


DEFAULT_SETTINGS = Settings(
    DEFAULT_CALENDAR, DEFAULT_AUTO_CLOSE_DAYS, DEFAULT_MERGE_WINDOW_HOURS, DEFAULT_LOW_RATING
)


async def load_settings(session: AsyncSession) -> Settings:
    row = await session.scalar(select(RequestSettings))
    if row is None:
        return DEFAULT_SETTINGS
    return Settings(
        parse_calendar(row.calendar),
        row.auto_close_days,
        row.merge_window_hours,
        row.low_rating_threshold,
    )


def sla_rule(category: RequestCategory, priority: RequestPriority, settings: Settings) -> SlaRule:
    hour_type = HourType(category.hour_type)
    return SlaRule(
        hour_type,
        hours_of(category)[priority],
        settings.calendar if hour_type is HourType.BUSINESS else None,
    )


@dataclass(frozen=True, slots=True)
class SettingsInput:
    categories: dict[str, dict[str, object]]  # anahtar → {hour_type, resolve_hours}
    calendar: object
    auto_close_days: object
    merge_window_hours: object
    low_rating_threshold: object


async def save_settings(session: AsyncSession, data: SettingsInput) -> Settings:
    """Servis isteği 47 `PUT /settings/requests`: kategori SLA'ları + takvim + süreler."""
    errors: dict[str, str] = {}
    known = {c.key: c for c in await categories(session)}
    updates: list[tuple[RequestCategory, HourType, dict[RequestPriority, int]]] = []
    for key, value in data.categories.items():
        category = known.get(key)
        if category is None:
            errors[f"categories.{key}"] = "Kategori bulunamadı."
            continue
        raw_type = value.get("hour_type")
        if raw_type not in {h.value for h in HourType}:
            errors[f"categories.{key}.hour_type"] = "7/24 ya da çalışma saati seçin."
            continue
        try:
            hours = check_hours(value.get("resolve_hours"), f"categories.{key}")
        except RuleError as exc:
            errors.update(exc.fields)
            continue
        updates.append((category, HourType(str(raw_type)), hours))
    checks = (
        ("calendar", lambda: parse_calendar(data.calendar)),
        ("auto_close_days", lambda: check_range(
            data.auto_close_days, "auto_close_days", AUTO_CLOSE_MIN, AUTO_CLOSE_MAX, "gün")),
        ("merge_window_hours", lambda: check_range(
            data.merge_window_hours, "merge_window_hours", MERGE_WINDOW_MIN, MERGE_WINDOW_MAX,
            "saat")),
        ("low_rating_threshold", lambda: check_range(
            data.low_rating_threshold, "low_rating_threshold", LOW_RATING_MIN, LOW_RATING_MAX,
            "puan")),
    )  # fmt: skip
    values: dict[str, object] = {}
    for name, check in checks:
        try:
            values[name] = check()
        except RuleError as exc:
            errors.update(exc.fields)
    if errors:
        raise RuleError(errors)
    for category, hour_type, hours in updates:
        category.hour_type = hour_type.value
        _set_hours(category, hours)
    calendar = cast(Calendar, values["calendar"])  # parse_calendar döndürür
    row = await session.scalar(select(RequestSettings))
    if row is None:
        row = RequestSettings()
        session.add(row)
    row.calendar = calendar_json(calendar)
    row.auto_close_days = int(str(values["auto_close_days"]))
    row.merge_window_hours = int(str(values["merge_window_hours"]))
    row.low_rating_threshold = int(str(values["low_rating_threshold"]))
    await session.flush()
    return await load_settings(session)


# --- Hazır yanıtlar --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateData:
    title: str | None
    category: str | None
    body: str | None
    is_active: bool = True


async def templates(session: AsyncSession) -> list[ReplyTemplate]:
    return list(
        await session.scalars(
            select(ReplyTemplate).order_by(ReplyTemplate.is_active.desc(), ReplyTemplate.title)
        )
    )


async def save_template(
    session: AsyncSession, data: TemplateData, *, template: ReplyTemplate | None
) -> ReplyTemplate:
    errors: dict[str, str] = {}
    title = " ".join((data.title or "").split())
    body = (data.body or "").strip()
    if not 2 <= len(title) <= TITLE_MAX:
        errors["title"] = "Başlık yazın."
    if not BODY_MIN <= len(body) <= BODY_MAX:
        errors["body"] = f"Metin {BODY_MIN}–{BODY_MAX} karakter olmalı."
    category = data.category or None
    if category is not None and not await session.scalar(
        select(RequestCategory.id).where(RequestCategory.key == category)
    ):
        errors["category"] = "Kategori geçersiz."
    if errors:
        raise RuleError(errors)
    if template is None:
        template = ReplyTemplate()
        session.add(template)
    template.title = title
    template.body = body
    template.category = category
    template.is_active = data.is_active
    await session.flush()
    return template


DEFAULT_TEMPLATES = (
    ("Talep alındı", "Merhaba {{sakin.ad}}, {{talep.no}} numaralı talebiniz ekibimize iletildi. "
     "En kısa sürede dönüş yapacağız."),
    ("Arıza giderildi", "Merhaba {{sakin.ad}}, {{talep.no}} numaralı talebinizdeki arıza "
     "giderildi. Sorun devam ederse lütfen bildirin."),
)  # fmt: skip


async def ensure_default_templates(session: AsyncSession) -> None:
    if await session.scalar(select(ReplyTemplate.id).limit(1)) is not None:
        return
    session.add_all(ReplyTemplate(title=t, body=b, is_active=True) for t, b in DEFAULT_TEMPLATES)
    await session.flush()
