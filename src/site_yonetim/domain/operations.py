"""Talep ve duyuru kuralları — saf (docs/03 §9, docs/06 §2.10–2.11).

Talep durum akışı (Yazılım Dokümanı HIZ-02 "Durumlar"; PM kararı K18, 25.10.2026 — "dokümandaki
akış"): Açık → Atandı → İşlemde → Beklemede → Çözüldü → Kapandı · İptal. Geçişler `TRANSITIONS`
tablosundadır; tablo dışı geçiş 409 `invalid_transition`.

- Atandı: talep bir kişiye/firmaya atanınca (atama ucu `open` talebi `assigned` yapar).
- Çözüldü → Kapandı: sakin onayı (HIZ-01 adım 7) ya da X gün yanıtsızlıkta zamanlanmış iş.
  Talebi bir sakin açtıysa personel doğrudan kapatamaz (409 `awaiting_confirmation`); onayı
  beklenecek kimse yoksa (personelin açtığı talep) personel kapatabilir.
- Çözüldü → İşlemde: personel çözümü yetersiz bulur. Sakin "devam ediyor" derse talep yeniden
  açılır (atanmışsa `assigned`, değilse `open`).
- Kapandı ve İptal son durumdur: yeniden açılmaz (dokümanda yeniden açma yok) — yeni talep açılır.
- İptal: gerekçeyle. Sakin kendi talebini **iş başlamadan** (`open`/`assigned`) iptal edebilir.

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


class RequestPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RequestStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
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
    CLOSED = "closed"
    CANCELLED = "cancelled"
    REOPENED = "reopened"
    FEEDBACK = "feedback"
    OUTAGE_LINKED = "outage_linked"
    OUTAGE_UNLINKED = "outage_unlinked"
    QUOTE = "quote"


STATUS_LABELS = {
    RequestStatus.OPEN: "Açık",
    RequestStatus.ASSIGNED: "Atandı",
    RequestStatus.IN_PROGRESS: "İşlemde",
    RequestStatus.WAITING: "Beklemede",
    RequestStatus.RESOLVED: "Çözüldü",
    RequestStatus.CLOSED: "Kapandı",
    RequestStatus.CANCELLED: "İptal edildi",
}
# Henüz bitmemiş talepler (pano, sakin ana sayfası, SLA takibi, birleştirme önerisi).
ACTIVE_STATUSES = (
    RequestStatus.OPEN,
    RequestStatus.ASSIGNED,
    RequestStatus.IN_PROGRESS,
    RequestStatus.WAITING,
)
FINAL_STATUSES = frozenset({RequestStatus.CLOSED, RequestStatus.CANCELLED})
# Sakinin kendi talebini iptal edebildiği durumlar (iş başlamadan).
RESIDENT_CANCELLABLE = frozenset({RequestStatus.OPEN, RequestStatus.ASSIGNED})

TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.OPEN: frozenset({RequestStatus.ASSIGNED, RequestStatus.CANCELLED}),
    RequestStatus.ASSIGNED: frozenset({RequestStatus.IN_PROGRESS, RequestStatus.CANCELLED}),
    RequestStatus.IN_PROGRESS: frozenset(
        {RequestStatus.WAITING, RequestStatus.RESOLVED, RequestStatus.CANCELLED}
    ),
    RequestStatus.WAITING: frozenset(
        {RequestStatus.IN_PROGRESS, RequestStatus.RESOLVED, RequestStatus.CANCELLED}
    ),
    RequestStatus.RESOLVED: frozenset({RequestStatus.IN_PROGRESS, RequestStatus.CLOSED}),
    RequestStatus.CLOSED: frozenset(),
    RequestStatus.CANCELLED: frozenset(),
}
REASON_REQUIRED = frozenset({RequestStatus.RESOLVED, RequestStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class RequestState:
    """Geçiş kararına giren talep bilgisi."""

    status: RequestStatus
    has_assignee: bool
    awaits_resident: bool  # talebi bir sakin açtı → kapanış sakin onayıyla


@dataclass(frozen=True, slots=True)
class StatusChange:
    event: RequestEventKind
    description: str
    resolved: bool  # `resolved_at` yazılır
    finished: bool  # `closed_at` yazılır (kapandı / iptal)


def next_statuses(state: RequestState) -> list[RequestStatus]:
    """Personelin seçebileceği durumlar (ekrandaki seçim ve pano sürükle-bırak)."""
    allowed = []
    for target in RequestStatus:
        if target not in TRANSITIONS[state.status]:
            continue
        if target is RequestStatus.ASSIGNED and not state.has_assignee:
            continue
        if target is RequestStatus.CLOSED and state.awaits_resident:
            continue
        allowed.append(target)
    return allowed


def clean_reason(value: str | None) -> str | None:
    return " ".join(value.split()) if value and value.strip() else None


def check_status_change(
    state: RequestState, new: RequestStatus, reason: str | None
) -> StatusChange:
    current = state.status
    if new is current:
        raise OperationRuleError(
            "status_unchanged", f"Talep zaten '{STATUS_LABELS[new]}' durumunda.", conflict=True
        )
    if current in FINAL_STATUSES:
        raise OperationRuleError(
            "request_finished",
            f"'{STATUS_LABELS[current]}' durumundaki talep yeniden açılamaz; "
            "gerekirse yeni talep açın.",
            conflict=True,
        )
    if new not in TRANSITIONS[current]:
        allowed = ", ".join(STATUS_LABELS[s] for s in RequestStatus if s in TRANSITIONS[current])
        raise OperationRuleError(
            "invalid_transition",
            f"'{STATUS_LABELS[current]}' talep '{STATUS_LABELS[new]}' yapılamaz. "
            f"Geçilebilecek durumlar: {allowed}.",
            conflict=True,
        )
    if new is RequestStatus.ASSIGNED and not state.has_assignee:
        raise OperationRuleError(
            "assignee_required", "Önce talebi bir kişiye ya da firmaya atayın.", conflict=True
        )
    if new is RequestStatus.CLOSED and state.awaits_resident:
        raise OperationRuleError(
            "awaiting_confirmation",
            "Talep sakin onayı bekliyor; sakin onaylayınca ya da yanıt gelmezse kendiliğinden "
            "kapanır.",
            conflict=True,
        )
    text = clean_reason(reason)
    if new in REASON_REQUIRED and text is None:
        message = (
            "Talebi iptal etmek için gerekçe yazın."
            if new is RequestStatus.CANCELLED
            else "Talebi çözüldü yapmak için ne yapıldığını yazın."
        )
        raise OperationRuleError("resolution_required", message, field="resolution")
    description = f"Durum: {STATUS_LABELS[current]} → {STATUS_LABELS[new]}"
    if new is RequestStatus.RESOLVED:
        description += f". Çözüm: {text}"
    elif new is RequestStatus.CANCELLED:
        description += f". Gerekçe: {text}"
    event = {
        RequestStatus.RESOLVED: RequestEventKind.RESOLVED,
        RequestStatus.CLOSED: RequestEventKind.CLOSED,
        RequestStatus.CANCELLED: RequestEventKind.CANCELLED,
    }.get(new, RequestEventKind.STATUS_CHANGED)
    return StatusChange(
        event,
        description,
        resolved=new is RequestStatus.RESOLVED,
        finished=new in FINAL_STATUSES,
    )


def reopen_status(has_assignee: bool) -> RequestStatus:
    """Sakin "sorun devam ediyor" dediğinde talebin döndüğü durum."""
    return RequestStatus.ASSIGNED if has_assignee else RequestStatus.OPEN


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
