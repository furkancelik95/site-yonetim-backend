"""Onay mekanizması — saf kurallar (Yazılım Dokümanı Bölüm 6, ONY-01..03).

Tüm modüllerin onayları tek servisten geçer (`services/approvals.py`); burada yalnız hesap var:
kural eşleştirme, tutar aralığı çakışması, gerekli onay sayısı (M-of-N), adım sonucu, süre.

Kurala bağlı kararlar (Furkan, 19.10.2026 — docs/12 K20):
- Eskalasyon hedefi kuralda açıkça seçilir; seçilmezse yalnız hatırlatma. Rol sırası (K20, rol
  düzeyi) yalnız **öneri** verir (`domain/roles.suggest_escalation`).
- Tutar aralığı **(alt, üst]**: alt sınır hariç, üst sınır dahil — dokümanın "≤ 10.000 → site
  yöneticisi; 10.000–100.000 → …; > 100.000 → …" örneğiyle aynı.
- Hiçbir kural eşleşmezse varsayılan: tek adım, site yöneticisi, 1/1 (ONY-01).
- Hiçbir durumda otomatik onay ya da otomatik ret yoktur (6.3).
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.access import SiteRole


class ActionType(StrEnum):
    """Onay gerektirebilen işlem tipleri (Bölüm 6.2 + ONY-01 hazır şablonları). Kural her tip için
    tanımlanabilir; talebi açabilmek için modülün yürütücüyü kaydetmiş olması gerekir."""

    CHARGE_RULE = "charge_rule"  # aidat kuralı
    CHARGE_RUN = "charge_run"  # tahakkuk
    CHARGE_RUN_REVERSAL = "charge_run_reversal"  # tahakkuk iptali / düzeltme
    REFUND = "refund"  # iade
    DEBT_WRITE_OFF = "debt_write_off"  # borç silme
    EXPENSE = "expense"  # gider
    EMERGENCY_EXPENSE = "emergency_expense"  # acil gider (sonradan onay)
    PURCHASE_REQUEST = "purchase_request"  # satın alma talebi
    PURCHASE_ORDER = "purchase_order"  # sipariş
    QUOTE_EXCEPTION = "quote_exception"  # teklif istisnası
    RESERVATION = "reservation"
    RENOVATION_PERMIT = "renovation_permit"  # tadilat / taşınma izni
    PAYMENT_PLAN = "payment_plan"  # ödeme planı
    PAID_SERVICE_TARIFF = "paid_service_tariff"  # ücretli hizmet tarifesi
    ROLE_CHANGE = "role_change"  # rol değişikliği (kritik izin)
    FINANCE_MODE_CHANGE = "finance_mode_change"  # finans modu geçişi
    REGISTRATION = "registration"  # kayıt başvurusu
    STOCK_COUNT_DIFFERENCE = "stock_count_difference"  # stok sayım farkı
    APPROVAL_RULE_CHANGE = "approval_rule_change"  # onay kuralı değişikliği
    LEAVE_REQUEST = "leave_request"  # izin talebi (IK-03)


ACTION_LABELS: dict[ActionType, str] = {
    ActionType.CHARGE_RULE: "Aidat kuralı",
    ActionType.CHARGE_RUN: "Tahakkuk",
    ActionType.CHARGE_RUN_REVERSAL: "Tahakkuk iptali / düzeltme",
    ActionType.REFUND: "İade",
    ActionType.DEBT_WRITE_OFF: "Borç silme",
    ActionType.EXPENSE: "Gider",
    ActionType.EMERGENCY_EXPENSE: "Acil gider (sonradan onay)",
    ActionType.PURCHASE_REQUEST: "Satın alma talebi",
    ActionType.PURCHASE_ORDER: "Sipariş",
    ActionType.QUOTE_EXCEPTION: "Teklif istisnası",
    ActionType.RESERVATION: "Rezervasyon",
    ActionType.RENOVATION_PERMIT: "Tadilat / taşınma izni",
    ActionType.PAYMENT_PLAN: "Ödeme planı",
    ActionType.PAID_SERVICE_TARIFF: "Ücretli hizmet tarifesi",
    ActionType.ROLE_CHANGE: "Rol değişikliği",
    ActionType.FINANCE_MODE_CHANGE: "Finans modu geçişi",
    ActionType.REGISTRATION: "Kayıt başvurusu",
    ActionType.STOCK_COUNT_DIFFERENCE: "Stok sayım farkı",
    ActionType.APPROVAL_RULE_CHANGE: "Onay kuralı değişikliği",
    ActionType.LEAVE_REQUEST: "İzin talebi",
}


class StepMode(StrEnum):
    SEQUENTIAL = "sequential"  # adımlar sırayla
    PARALLEL = "parallel"  # adımlar aynı anda


class RejectBehavior(StrEnum):
    SINGLE_REJECTS = "single_rejects"  # tek ret tüm talebi reddeder
    COUNTS = "counts"  # ret M-of-N hesabına sayılır


class QuorumKind(StrEnum):
    ANY = "any"  # herhangi biri = 1/N
    ALL = "all"  # hepsi = N/N
    MAJORITY = "majority"  # çoğunluk = ⌊N/2⌋ + 1
    COUNT = "count"  # sabit M


class DayBasis(StrEnum):
    CALENDAR = "calendar"
    BUSINESS = "business"  # hafta içi; resmî tatil takvimi (S-03) henüz yok


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISION = "revision"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    REVISION_REQUESTED = "revision_requested"
    APPROVED = "approved"  # onaylandı, yürütme bekliyor (aynı işlemde yürütülür)
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"  # onaylandı – yürütülemedi
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    MODULE_DISABLED = "module_disabled"


OPEN_STATUSES = frozenset({ApprovalStatus.PENDING, ApprovalStatus.REVISION_REQUESTED})
REMIND_MIN, REMIND_MAX = 1, 30
STEPS_MAX = 5


@dataclass(frozen=True, slots=True)
class Quorum:
    kind: QuorumKind
    count: int | None = None  # yalnız COUNT

    def required(self, n: int) -> int:
        """N onaycı için gereken onay sayısı M."""
        if n <= 0:
            return 1  # onaycı yoksa bile en az bir onay gerekir — adım tıkalı sayılır
        if self.kind is QuorumKind.ANY:
            return 1
        if self.kind is QuorumKind.ALL:
            return n
        if self.kind is QuorumKind.MAJORITY:
            return math.floor(n / 2) + 1
        return self.count or 1

    def label(self, n: int) -> str:
        return f"{self.required(n)}/{n}"


@dataclass(frozen=True, slots=True)
class Step:
    roles: tuple[str, ...]  # rol adları (hazır şablon ya da siteye özel rol)
    quorum: Quorum


@dataclass(frozen=True, slots=True)
class Condition:
    """(amount_min, amount_max] aralığı ve kaynak (ör. "emergency"); boş = koşul yok."""

    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    source: str | None = None

    def matches(self, amount: Decimal | None, source: str | None) -> bool:
        if self.source is not None and self.source != source:
            return False
        if self.amount_min is None and self.amount_max is None:
            return True
        if amount is None:
            return False  # tutar koşullu kural tutarsız talebe uygulanmaz
        if self.amount_min is not None and amount <= self.amount_min:
            return False
        return not (self.amount_max is not None and amount > self.amount_max)

    @property
    def specificity(self) -> int:
        """Eşleşen birden çok kural varsa en dar olan uygulanır: kaynak + tutar > yalnız biri."""
        has_amount = self.amount_min is not None or self.amount_max is not None
        return int(self.source is not None) * 2 + int(has_amount)


def ranges_overlap(a: Condition, b: Condition) -> bool:
    """Aynı işlem tipinde **aynı kaynak koşulundaki** iki kuralın tutar aralığı çakışır mı?"""
    if a.source != b.source:
        return False
    a_low = a.amount_min if a.amount_min is not None else Decimal("-Infinity")
    a_high = a.amount_max if a.amount_max is not None else Decimal("Infinity")
    b_low = b.amount_min if b.amount_min is not None else Decimal("-Infinity")
    b_high = b.amount_max if b.amount_max is not None else Decimal("Infinity")
    return a_low < b_high and b_low < a_high  # (alt, üst] aralıkları


def coverage_gaps(conditions: list[Condition]) -> list[tuple[Decimal | None, Decimal | None]]:
    """Kaynak koşulsuz tutar aralıklarının bıraktığı boşluklar (uyarı: "bu aralık için kural
    yok; varsayılan kural uygulanır"). Tutar koşulsuz bir kural varsa boşluk yoktur."""
    ranged = sorted(
        (c for c in conditions if c.source is None),
        key=lambda c: c.amount_min if c.amount_min is not None else Decimal("-Infinity"),
    )
    if not ranged or any(c.amount_min is None and c.amount_max is None for c in ranged):
        return []
    gaps: list[tuple[Decimal | None, Decimal | None]] = []
    cursor: Decimal | None = None  # şu ana kadar kapsanan üst sınır; None = -∞
    first = True
    for c in ranged:
        if first:
            if c.amount_min is not None and c.amount_min > 0:
                gaps.append((None, c.amount_min))
            first = False
        elif cursor is not None and c.amount_min is not None and c.amount_min > cursor:
            gaps.append((cursor, c.amount_min))
        if c.amount_max is None:
            return gaps
        cursor = c.amount_max if cursor is None else max(cursor, c.amount_max)
    gaps.append((cursor, None))
    return gaps


DEFAULT_STEPS = (Step((SiteRole.MANAGER.value,), Quorum(QuorumKind.ANY)),)


# --- Adım sonucu --------------------------------------------------------------------------


class StepOutcome(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def step_outcome(
    *, approvals: int, rejections: int, required: int, behavior: RejectBehavior
) -> StepOutcome:
    """M onaya ulaşınca onay; "tek ret reddeder"de ilk ret reddeder. "Ret hesaba sayılır"da
    rettler M'yi engellese bile adım beklemede kalır — otomatik ret yok (6.3)."""
    if behavior is RejectBehavior.SINGLE_REJECTS and rejections > 0:
        return StepOutcome.REJECTED
    if approvals >= required:
        return StepOutcome.APPROVED
    return StepOutcome.PENDING


def quorum_unreachable(*, approvals: int, rejections: int, eligible: int, required: int) -> bool:
    """Kalan onaycılar hepsi onaylasa bile M'ye ulaşılamaz mı? (ekranda uyarı)."""
    return approvals + (eligible - approvals - rejections) < required


# --- Süre ---------------------------------------------------------------------------------


def add_days(start: date, days: int, basis: DayBasis) -> date:
    """İş günüyse cumartesi ve pazar sayılmaz. Resmî tatiller ortak takvim servisi (S-03)
    gelince eklenir; o zamana kadar hafta içi her gün iş günüdür."""
    if basis is DayBasis.CALENDAR:
        return start + timedelta(days=days)
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def check_timing(remind_after: int, escalate_after: int) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not REMIND_MIN <= remind_after <= REMIND_MAX:
        errors["remind_after_days"] = f"Hatırlatma {REMIND_MIN}–{REMIND_MAX} gün olmalı."
    if escalate_after <= remind_after:
        errors["escalate_after_days"] = "Eskalasyon günü hatırlatma gününden sonra olmalı."
    return errors
