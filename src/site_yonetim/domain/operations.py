"""Talep ve duyuru kuralları — saf (docs/03 §9, docs/06 §2.10–2.11).

Talep durum geçişleri dokümanda tanımlı değil (docs/12 K18): şimdilik her durumdan her duruma
geçilebilir, yalnız aynı duruma geçiş ve çözüm metni olmadan `resolved`/`closed` reddedilir.
Her değişiklik değişmez olay geçmişine yazılır.
"""

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from site_yonetim.domain.structure import PartyRole, is_active_on


class OperationRuleError(Exception):
    """`conflict` → 409, yoksa 422."""

    def __init__(
        self, code: str, message: str, *, field: str | None = None, conflict: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.conflict = conflict


# --- Talep --------------------------------------------------------------------------


class RequestCategory(StrEnum):
    OTHER = "other"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    ELEVATOR = "elevator"
    HEATING = "heating"
    CLEANING = "cleaning"
    SECURITY = "security"
    GARDEN = "garden"
    COMMON_AREA = "common_area"


class RequestPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RequestStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class RequestEventKind(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    ASSIGNED = "assigned"
    COMMENT = "comment"
    RESOLVED = "resolved"
    DEPARTMENT_CHANGED = "department_changed"


STATUS_LABELS = {
    RequestStatus.OPEN: "Açık",
    RequestStatus.IN_PROGRESS: "İşlemde",
    RequestStatus.WAITING: "Beklemede",
    RequestStatus.RESOLVED: "Çözüldü",
    RequestStatus.CLOSED: "Kapandı",
    RequestStatus.CANCELLED: "İptal edildi",
}
RESOLUTION_REQUIRED = frozenset({RequestStatus.RESOLVED, RequestStatus.CLOSED})


@dataclass(frozen=True, slots=True)
class StatusChange:
    event: RequestEventKind
    description: str
    resolved: bool  # `resolved_at` dolu olmalı mı


def check_status_change(
    current: RequestStatus, new: RequestStatus, resolution: str | None
) -> StatusChange:
    if new is current:
        raise OperationRuleError(
            "status_unchanged", f"Talep zaten '{STATUS_LABELS[new]}' durumunda.", conflict=True
        )
    if new in RESOLUTION_REQUIRED and not (resolution and resolution.strip()):
        raise OperationRuleError(
            "resolution_required",
            "Talebi çözüldü ya da kapandı yapmak için ne yapıldığını yazın.",
            field="resolution",
        )
    description = f"Durum: {STATUS_LABELS[current]} → {STATUS_LABELS[new]}"
    if new in RESOLUTION_REQUIRED and resolution:
        description += f". Çözüm: {' '.join(resolution.split())}"
    event = (
        RequestEventKind.RESOLVED
        if new is RequestStatus.RESOLVED
        else (RequestEventKind.STATUS_CHANGED)
    )
    return StatusChange(event, description, new in RESOLUTION_REQUIRED)


# --- Duyuru -------------------------------------------------------------------------


class Importance(StrEnum):
    NORMAL = "normal"
    IMPORTANT = "important"
    CRITICAL = "critical"


class Audience(StrEnum):
    ALL_RESIDENTS = "all_residents"
    BLOCKS = "blocks"
    OWNERS_ONLY = "owners_only"
    TENANTS_ONLY = "tenants_only"
    DEBTORS_ONLY = "debtors_only"


class Channel(StrEnum):
    IN_APP = "in_app"
    WEB_PUSH = "web_push"
    EMAIL = "email"
    SMS = "sms"


# Sakin sayılan taraflar: malik, kiracı, oturan. Vekil duyuru almaz.
RESIDENT_ROLES = frozenset({PartyRole.OWNER, PartyRole.TENANT, PartyRole.RESIDENT})


@dataclass(frozen=True, slots=True)
class PartyRef:
    person_id: uuid.UUID
    unit_id: uuid.UUID
    block_id: uuid.UUID
    role: PartyRole
    start_date: date
    end_date: date | None


def recipients(
    parties: Iterable[PartyRef],
    *,
    audience: Audience,
    today: date,
    block_ids: Collection[uuid.UUID] = (),
    debtor_person_ids: Collection[uuid.UUID] = (),
) -> set[uuid.UUID]:
    """Duyurunun hedef kişileri: bugün etkin malik/kiracı/oturanlar arasından süzülür.

    `debtors_only`: borçlu (bakiyesi > 0) bir cari hesabı olan etkin sakinler.
    """
    active = [
        p
        for p in parties
        if p.role in RESIDENT_ROLES and is_active_on(p.start_date, p.end_date, today)
    ]
    match audience:
        case Audience.ALL_RESIDENTS:
            chosen = active
        case Audience.BLOCKS:
            chosen = [p for p in active if p.block_id in block_ids]
        case Audience.OWNERS_ONLY:
            chosen = [p for p in active if p.role is PartyRole.OWNER]
        case Audience.TENANTS_ONLY:
            chosen = [p for p in active if p.role is PartyRole.TENANT]
        case Audience.DEBTORS_ONLY:
            chosen = [p for p in active if p.person_id in debtor_person_ids]
    return {p.person_id for p in chosen}


def channels_of(requested: Iterable[Channel]) -> list[Channel]:
    """Uygulama içi bildirim her zaman vardır; diğerleri isteğe bağlı (sıra korunur)."""
    ordered = [Channel.IN_APP]
    ordered.extend(c for c in dict.fromkeys(requested) if c is not Channel.IN_APP)
    return ordered
