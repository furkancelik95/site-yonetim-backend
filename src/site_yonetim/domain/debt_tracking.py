"""Borç takibi ve ödeme planı — saf kurallar (Yazılım Dokümanı FIN-12, §8.9, §8.12; servis
istekleri 29–30).

- Yaşlandırma kovası hesabın gecikme gününe göre (borçlu listesiyle aynı hesap, docs/04 §13).
- Hatırlatma kademeleri finans parametresidir (vade +3 bildirim, +10 SMS, +30 yönetici araması,
  +60 ihtarname hazırlığı). Aynı açık vade için her kademe bir kez.
- Ödeme planı taksitleri eşittir; kuruş artığı **ilk taksitlere** (en büyük kalan — `distribute`):
  1.000,00 / 3 = 333,34 + 333,33 + 333,33. Taksitler ayda bir.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import EPSILON, ZERO, distribute, round_money
from site_yonetim.domain.text import format_money_tr

NOTE_MAX = 500
INSTALLMENTS_MIN, INSTALLMENTS_MAX = 2, 36


class AgingBucket(StrEnum):
    D0_30 = "0_30"
    D31_60 = "31_60"
    D61_90 = "61_90"
    D90_PLUS = "90_plus"


BUCKET_LABELS = {
    AgingBucket.D0_30: "0–30 gün",
    AgingBucket.D31_60: "31–60 gün",
    AgingBucket.D61_90: "61–90 gün",
    AgingBucket.D90_PLUS: "90+ gün",
}


def bucket_of(overdue_days: int) -> AgingBucket:
    if overdue_days <= 30:
        return AgingBucket.D0_30
    if overdue_days <= 60:
        return AgingBucket.D31_60
    if overdue_days <= 90:
        return AgingBucket.D61_90
    return AgingBucket.D90_PLUS


class ActionKind(StrEnum):
    REMINDER = "reminder"
    CALL = "call"
    NOTE = "note"


class CollectionChannel(StrEnum):
    APP = "app"
    SMS = "sms"
    PHONE = "phone"


class ReminderStage(StrEnum):
    APP = "app"  # 1. kademe: uygulama bildirimi
    SMS = "sms"  # 2. kademe: SMS
    CALL = "call"  # 3. kademe: yöneticiye arama görevi
    DEMAND_LETTER = "demand_letter"  # ihtarname hazırlığı


def due_stages(overdue_days: int, reminder_days: list[int], letter_day: int) -> list[ReminderStage]:
    """Gecikme gününe kadar gelinmiş kademeler (eskiden yeniye)."""
    thresholds = [
        (reminder_days[0], ReminderStage.APP),
        (reminder_days[1], ReminderStage.SMS),
        (reminder_days[2], ReminderStage.CALL),
        (letter_day, ReminderStage.DEMAND_LETTER),
    ]
    return [stage for day, stage in thresholds if overdue_days >= day]


def check_action(
    kind: str | None, channel: str | None, note: str | None
) -> tuple[ActionKind, CollectionChannel | None, str | None]:
    errors: dict[str, str] = {}
    text = " ".join((note or "").split())[:NOTE_MAX] or None
    action = ActionKind(kind) if kind in {k.value for k in ActionKind} else None
    if action is None:
        errors["kind"] = "İşlem türünü seçin."
    picked: CollectionChannel | None = None
    if action is ActionKind.REMINDER:
        if channel not in (CollectionChannel.APP.value, CollectionChannel.SMS.value):
            errors["channel"] = "Kanal seçin (uygulama ya da SMS)."
        else:
            picked = CollectionChannel(channel)
    elif action is ActionKind.CALL:
        picked = CollectionChannel.PHONE
    if action in (ActionKind.CALL, ActionKind.NOTE) and (text is None or len(text) < 3):
        errors["note"] = "Kısa bir not yazın."
    if errors or action is None:
        raise FormFieldError(errors)
    return action, picked, text


# --- İhtarname ---------------------------------------------------------------------------


class LetterMethod(StrEnum):
    HAND = "hand"  # elden imzalı
    REGISTERED_MAIL = "registered_mail"  # iadeli taahhütlü
    NOTARY = "notary"  # noter


class LetterStatus(StrEnum):
    APPROVAL_PENDING = "approval_pending"
    SENT = "sent"
    SERVED = "served"
    REJECTED = "rejected"


def check_letter(
    method: str | None, sent_on: date | None, tracking_no: str | None, today: date
) -> tuple[LetterMethod, date, str | None]:
    errors: dict[str, str] = {}
    picked = LetterMethod(method) if method in {m.value for m in LetterMethod} else None
    if picked is None:
        errors["method"] = "Gönderim yöntemini seçin."
    if sent_on is None:
        errors["sent_on"] = "Gönderim tarihini girin."
    elif sent_on > today:
        errors["sent_on"] = "Gelecek tarih olamaz."
    tracking = " ".join((tracking_no or "").split())[:100] or None
    if picked in (LetterMethod.REGISTERED_MAIL, LetterMethod.NOTARY) and tracking is None:
        errors["tracking_no"] = "Takip / yevmiye numarasını girin."
    if errors or picked is None or sent_on is None:
        raise FormFieldError(errors)
    return picked, sent_on, tracking


def check_served(served_on: date, sent_on: date, today: date) -> None:
    if served_on < sent_on:
        raise FormFieldError({"served_on": "Tebliğ, gönderimden önce olamaz."})
    if served_on > today:
        raise FormFieldError({"served_on": "Gelecek tarih olamaz."})


# --- Ödeme planı -------------------------------------------------------------------------


class PlanStatus(StrEnum):
    APPROVAL_PENDING = "approval_pending"
    ACTIVE = "active"
    BROKEN = "broken"  # bir taksit vade + tolerans geçip ödenmedi
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"  # onay reddedildi / geri çekildi


class InstallmentStatus(StrEnum):
    PAID = "paid"
    PARTIAL = "partial"
    OPEN = "open"
    OVERDUE = "overdue"


def add_months(day: date, months: int) -> date:
    """Ayda bir vade: ay sonunu aşan gün ayın son gününe çekilir (31 Ocak + 1 ay → 28 Şubat)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True, slots=True)
class PlannedInstallment:
    no: int
    due_date: date
    amount: Decimal


def schedule(total: Decimal, count: int, first_due: date) -> list[PlannedInstallment]:
    """Eşit taksit; kuruş artığı ilk taksitlere (toplam her zaman korunur)."""
    amounts = distribute(total, [Decimal(1)] * count)
    return [
        PlannedInstallment(i + 1, add_months(first_due, i), amount)
        for i, amount in enumerate(amounts)
    ]


def check_plan(
    amount: Decimal | None, count: int | None, first_due: date | None, open_total: Decimal,
    today: date,
) -> None:  # fmt: skip
    errors: dict[str, str] = {}
    if amount is None or amount <= ZERO or amount != round_money(amount):
        errors["amount"] = "Yapılandırılacak tutarı girin."
    elif amount > open_total + EPSILON:
        errors["amount"] = f"Tutar açık bakiyeyi ({format_money_tr(open_total)}) aşamaz."
    if count is None or not INSTALLMENTS_MIN <= count <= INSTALLMENTS_MAX:
        errors["installment_count"] = "Taksit sayısı 2–36 arası."
    if first_due is None:
        errors["first_due_date"] = "İlk vadeyi girin."
    elif first_due < today:
        errors["first_due_date"] = "İlk vade bugünden önce olamaz."
    if errors:
        raise FormFieldError(errors)


def installment_status(
    amount: Decimal, paid: Decimal, due: date, tolerance_days: int, today: date
) -> InstallmentStatus:
    if paid >= amount - EPSILON:
        return InstallmentStatus.PAID
    if today > due + timedelta(days=tolerance_days):
        return InstallmentStatus.OVERDUE
    if paid > EPSILON:
        return InstallmentStatus.PARTIAL
    return InstallmentStatus.OPEN


def plan_status(installments: list[InstallmentStatus]) -> PlanStatus:
    if installments and all(s is InstallmentStatus.PAID for s in installments):
        return PlanStatus.COMPLETED
    if any(s is InstallmentStatus.OVERDUE for s in installments):
        return PlanStatus.BROKEN
    return PlanStatus.ACTIVE


def check_cancel_reason(reason: str | None) -> str:
    text = " ".join((reason or "").split())[:NOTE_MAX]
    if len(text) < 5:
        raise FinanceRuleError("validation", "Gerekçe yazın (en az 5 karakter).", field="reason")
    return text
