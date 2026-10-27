"""Bildirim/duyuru şablonları (ILT-02), acil bilgi kartı (ILT-06) ve acil durum saklama ayarı.
Açık site kapsamında; commit çağırana ait.

- Şablon: sistem şablonu sürüm 1'dir; site her kaydettiğinde yeni sürüm satırı yazılır
  (değişmez tablo). Sistem metnine dönüş de yeni sürümdür. Yeni gönderimler son sürümü kullanır.
- Kart: site başına tek satır; her yayında sürüm artar. Satır yoksa doküman varsayılanları
  (112 ve kısa talimatlar) sürüm 0 olarak döner.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.communication import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_PHONES,
    INSTRUCTION_KEYS,
    MUSTER_POINTS_MAX,
    PHONES_MAX,
    RETENTION_DAYS_DEFAULT,
    RETENTION_DAYS_MAX,
    RETENTION_DAYS_MIN,
    SYSTEM_BY_CODE,
    SYSTEM_TEMPLATES,
    TemplateDef,
    Variant,
    check_variants,
    clean_phone,
    valid_map,
    valid_phone,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Block, EmergencyInfoCard, EmergencySettings
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.models.communication import NotificationTemplateVersion

# --- Şablonlar ----------------------------------------------------------------------------

SYSTEM_AUTHOR = "Sistem"


@dataclass(frozen=True, slots=True)
class TemplateState:
    defn: TemplateDef
    variants: list[Variant]
    version: int
    customized: bool
    history: list[tuple[int, datetime | None, str]]  # (sürüm, zaman, kim)


def _variants(raw: list[dict[str, Any]]) -> list[Variant]:
    return [Variant(v["lang"], v.get("subject"), v["body"]) for v in raw]


async def templates(session: AsyncSession) -> list[TemplateState]:
    rows = list(
        await session.scalars(
            select(NotificationTemplateVersion).order_by(
                NotificationTemplateVersion.code, NotificationTemplateVersion.version
            )
        )
    )
    by_code: dict[str, list[NotificationTemplateVersion]] = {}
    for row in rows:
        by_code.setdefault(row.code, []).append(row)
    found = []
    for defn in SYSTEM_TEMPLATES:
        versions = by_code.get(defn.code, [])
        history: list[tuple[int, datetime | None, str]] = [(1, None, SYSTEM_AUTHOR)]
        history += [(v.version, v.created_at, v.created_by) for v in versions]
        last = versions[-1] if versions else None
        found.append(
            TemplateState(
                defn,
                _variants(last.variants) if last else list(defn.variants),
                last.version if last else 1,
                last.customized if last else False,
                history,
            )
        )
    return found


async def _next_version(session: AsyncSession, code: str) -> int:
    current = await session.scalar(
        select(func.max(NotificationTemplateVersion.version)).where(
            NotificationTemplateVersion.code == code
        )
    )
    return (current or 1) + 1


async def save_template(
    session: AsyncSession, code: str, variants: list[Variant], *, by: str
) -> int:
    defn = SYSTEM_BY_CODE.get(code)
    if defn is None:
        raise LookupError("Şablon bulunamadı.")
    cleaned = [Variant(v.lang, (v.subject or "").strip() or None, v.body.strip()) for v in variants]
    errors = check_variants(defn, cleaned)
    if errors:
        raise FormFieldError(errors)
    version = await _next_version(session, code)
    session.add(
        NotificationTemplateVersion(
            code=code,
            version=version,
            variants=[{"lang": v.lang, "subject": v.subject, "body": v.body} for v in cleaned],
            customized=True,
            created_by=by,
        )
    )
    await session.flush()
    return version


async def reset_template(session: AsyncSession, code: str, *, by: str) -> int:
    defn = SYSTEM_BY_CODE.get(code)
    if defn is None:
        raise LookupError("Şablon bulunamadı.")
    version = await _next_version(session, code)
    session.add(
        NotificationTemplateVersion(
            code=code,
            version=version,
            variants=[
                {"lang": v.lang, "subject": v.subject, "body": v.body} for v in defn.variants
            ],
            customized=False,
            created_by=by,
        )
    )
    await session.flush()
    return version


# --- Acil durum saklama ayarı (K8) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    retention_days: int
    auto_purge: bool
    updated_by: str | None
    updated_at: datetime | None


async def retention(session: AsyncSession) -> RetentionSettings:
    row = await session.scalar(select(EmergencySettings))
    if row is None:
        return RetentionSettings(RETENTION_DAYS_DEFAULT, False, None, None)
    return RetentionSettings(
        row.retention_days, row.auto_purge, row.updated_by, row.updated_at or row.created_at
    )


async def save_retention(
    session: AsyncSession, *, retention_days: int, auto_purge: bool, by: str
) -> None:
    if not RETENTION_DAYS_MIN <= retention_days <= RETENTION_DAYS_MAX:
        raise FormFieldError(
            {"retention_days": f"{RETENTION_DAYS_MIN}–{RETENTION_DAYS_MAX} gün arası yazın."}
        )
    row = await session.scalar(select(EmergencySettings).with_for_update())
    if row is None:
        row = EmergencySettings(retention_days=retention_days, auto_purge=auto_purge)
        session.add(row)
    row.retention_days = retention_days
    row.auto_purge = auto_purge
    row.updated_by = by
    await session.flush()


# --- Acil bilgi kartı (ILT-06) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Card:
    muster_points: list[dict[str, Any]]
    phones: list[dict[str, Any]]
    instructions: dict[str, str]
    block_notes: list[dict[str, Any]]
    version: int
    updated_at: datetime | None
    updated_by: str | None


async def card(session: AsyncSession) -> Card:
    row = await session.scalar(select(EmergencyInfoCard))
    if row is None:
        return Card([], [dict(p) for p in DEFAULT_PHONES], dict(DEFAULT_INSTRUCTIONS), [], 0,
                    None, None)  # fmt: skip
    return Card(
        row.muster_points,
        row.phones,
        row.instructions,
        row.block_notes,
        row.version,
        row.updated_at or row.created_at,
        row.updated_by,
    )


@dataclass(frozen=True, slots=True)
class CardInput:
    muster_points: list[dict[str, str | None]]  # id, name, directions, map
    phones: list[dict[str, str]]
    instructions: dict[str, str]
    block_notes: list[dict[str, str]]  # block_id, note


async def publish_card(session: AsyncSession, data: CardInput, *, by: str) -> int:
    """Doğrular ve yeni sürüm olarak yayınlar. Hata anahtarları servis isteği 74'teki gibi."""
    errors: dict[str, str] = {}
    points: list[dict[str, Any]] = []
    for i, m in enumerate(data.muster_points[:MUSTER_POINTS_MAX]):
        name = " ".join((m.get("name") or "").split())
        directions = " ".join((m.get("directions") or "").split())
        image = m.get("map") or None
        if not 2 <= len(name) <= 60:
            errors[f"muster_points.{i}.name"] = "Alan adını yazın (2–60 karakter)."
        if len(directions) > 200:
            errors[f"muster_points.{i}.directions"] = "Tarif en fazla 200 karakter."
        if image is not None and not valid_map(image):
            errors[f"muster_points.{i}.map"] = (
                "Kroki PNG, JPEG ya da WEBP olmalı (en fazla ~300 KB)."
            )
        try:
            point_id = str(uuid.UUID(str(m.get("id")))) if m.get("id") else str(uuid.uuid7())
        except ValueError:
            point_id = str(uuid.uuid7())
        points.append({"id": point_id, "name": name, "directions": directions, "map": image})
    if not points:
        errors["muster_points"] = "En az bir toplanma alanı tanımlayın."
    elif len({p["name"].casefold() for p in points}) != len(points):
        errors["muster_points"] = "Aynı adlı iki toplanma alanı var."
    phones: list[dict[str, Any]] = []
    for i, p in enumerate(data.phones[:PHONES_MAX]):
        label = " ".join((p.get("label") or "").split())
        number = clean_phone(p.get("number") or "")
        if not 1 <= len(label) <= 40:
            errors[f"phones.{i}.label"] = "Ad yazın."
        if not valid_phone(number):
            errors[f"phones.{i}.number"] = "Geçerli bir numara yazın."
        phones.append({"label": label, "number": number})
    instructions = dict(DEFAULT_INSTRUCTIONS)
    for key in INSTRUCTION_KEYS:
        if key in data.instructions:
            text = data.instructions[key].strip()
            if len(text) > 400:
                errors[f"instructions.{key}"] = "Talimat en fazla 400 karakter."
            instructions[key] = text
    names = dict((await session.execute(select(Block.id, Block.name))).all())
    notes: list[dict[str, Any]] = []
    for i, n in enumerate(data.block_notes):
        note = " ".join((n.get("note") or "").split())
        if not note:
            continue
        try:
            block_id = uuid.UUID(n.get("block_id") or "")
        except ValueError:
            block_id = None
        if block_id not in names:
            errors[f"block_notes.{i}.block_id"] = "Blok bu sitede bulunamadı."
            continue
        if len(note) > 200:
            errors[f"block_notes.{i}.note"] = "Not en fazla 200 karakter."
        notes.append({"block_id": str(block_id), "block_name": names[block_id], "note": note})
    if errors:
        raise FormFieldError(errors)
    row = await session.scalar(select(EmergencyInfoCard).with_for_update())
    if row is None:
        row = EmergencyInfoCard(version=0)
        session.add(row)
    row.muster_points, row.phones, row.instructions = points, phones, instructions
    row.block_notes = notes
    row.version += 1
    row.updated_by = by
    await session.flush()
    # Kroki görselleri denetim kaydına girmez; özet yazılır.
    record(
        session,
        AuditAction.UPDATE,
        "emergency_info_cards",
        row.id,
        {"version": row.version, "muster_points": [p["name"] for p in points],
         "phones": len(phones), "block_notes": len(notes)},
    )  # fmt: skip
    return row.version
