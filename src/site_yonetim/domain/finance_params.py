"""Finans parametreleri — saf kurallar (Yazılım Dokümanı FIN-16, §8.7; servis isteği 33).

Her değişiklik yürürlük dönemiyle yeni **sürümdür**; bir dönemde geçerli değer, yürürlüğü o
döneme kadar başlamış en son (onaylı) sürümdür. Geçmiş dönemler değişmez. **Finansal etkili**
parametreler en erken bir sonraki dönemden geçerli olur ve "Finans parametresi" onay kuralından
geçer.

PM kararlarının ayar noktaları (P03b tahakkuk motoru bunları okur — `services/charging.prepare`,
`services/late_fees`, `services/payments`):
- K9 kıst `proration`: `day` (varsayılan, PM kararı — doküman "ay bazlı" diyordu) | `month`
- K25 vade günü `due_day`: varsayılan dokümandaki gibi ayın 10'u; site değiştirebilir
- K23 tahakkuk kaynağı `charge_source`: `budget_plan` | `billing_rule` (site seçer)
- K2 gecikme tazminatı `late_fee_*`: site kararıyla açılır, **varsayılan kapalı**; oran ve
  yöntem avukat görüşüne kadar parametre (KMK m.20 aylık %5 üst sınırı aşılamaz)
- K11 borçlu hizmet kısıtı `debtor_restriction_*`: site kararı, varsayılan kapalı; eşik tutar/gün
- K24 mahsup sırası `allocation_order`: varsayılan mevcut davranış (önce gecikme, TBK m.100)
"""

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from site_yonetim.domain.charging.periods import YearMonth


class Proration(StrEnum):
    DAY = "day"  # gün oranı (15/31 + 16/31)
    MONTH = "month"  # başladığı ve bittiği ay tam


class ChargeSource(StrEnum):
    BUDGET_PLAN = "budget_plan"  # işletme projesi
    BILLING_RULE = "billing_rule"  # aidat kuralı (FIN-02)


class LateFeeMethod(StrEnum):
    DAILY = "daily"  # KMK aylık %5 — gün bazlı oransal (oran × gün / 30)
    FULL_MONTH = "full_month"  # başlamış ay = tam ay


class AllocationOrder(StrEnum):
    LATE_FEE_FIRST = "late_fee_first"  # aynı vadede önce gecikme (TBK m.100) — bugünkü
    PRINCIPAL_FIRST = "principal_first"  # önce en eski ana borç, sonra gecikmesi


class BankAutoMatch(StrEnum):
    HIGH = "high"
    HIGH_MEDIUM = "high_medium"


DEFAULTS: dict[str, Any] = {
    "reminder_days": [3, 10, 30],
    "demand_letter_day": 60,
    "proration": Proration.DAY.value,
    "diff_threshold_percent": 10,
    "bank_auto_match": BankAutoMatch.HIGH.value,
    "plan_tolerance_days": 10,
    "plan_suspends_late_fee": True,
    "budget_warn_percent": 90,
    "due_day": 10,
    "charge_source": ChargeSource.BUDGET_PLAN.value,
    "late_fee_enabled": False,
    "late_fee_monthly_rate_percent": "5.00",
    "late_fee_method": LateFeeMethod.DAILY.value,
    "late_fee_grace_days": 5,
    "late_fee_cap_percent": 0,  # tavan: ana borcun %X'i (0 = tavan yok) — FIN-03
    "late_fee_minimum_amount": "0.00",  # bu tutarın altındaki gecikme yazılmaz
    "allocation_order": AllocationOrder.LATE_FEE_FIRST.value,
    "debtor_restriction_enabled": False,
    "debtor_restriction_min_amount": "0.00",
    "debtor_restriction_min_days": 90,
}

# En erken bir sonraki dönemden geçerli olur ve onay kuralından geçer (FIN-16 adım 2).
FINANCIAL = frozenset(
    {
        "proration",
        "plan_suspends_late_fee",
        "due_day",
        "charge_source",
        "late_fee_enabled",
        "late_fee_monthly_rate_percent",
        "late_fee_method",
        "late_fee_grace_days",
        "late_fee_cap_percent",
        "late_fee_minimum_amount",
        "allocation_order",
    }
)
LATE_FEE_RATE_MAX = Decimal("5.00")  # KMK m.20 — üstü için avukat görüşü (K2)

_CHOICES: dict[str, type[StrEnum]] = {
    "proration": Proration,
    "charge_source": ChargeSource,
    "late_fee_method": LateFeeMethod,
    "allocation_order": AllocationOrder,
    "bank_auto_match": BankAutoMatch,
}
_INT_RANGES: dict[str, tuple[int, int, str]] = {
    "diff_threshold_percent": (1, 100, "1–100 arası olmalı."),
    "plan_tolerance_days": (0, 60, "0–60 gün olmalı."),
    "budget_warn_percent": (50, 100, "50–100 arası olmalı."),
    "due_day": (1, 28, "Vade günü ayın 1'i ile 28'i arası olmalı."),
    "late_fee_grace_days": (0, 30, "Tolerans 0–30 gün olmalı."),
    "late_fee_cap_percent": (0, 100, "Tavan ana borcun %0–100'ü olmalı (0 = tavan yok)."),
    "debtor_restriction_min_days": (0, 365, "0–365 gün olmalı."),
}
_BOOLS = ("plan_suspends_late_fee", "late_fee_enabled", "debtor_restriction_enabled")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def merge(base: dict[str, Any], changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """`base` üzerine değişenleri uygular, doğrular. Dönüş: (yeni değerler, alan hataları).
    Bilinmeyen anahtar hata; eksik anahtar varsayılandan tamamlanır (eski sürümlerle uyum)."""
    values = {**DEFAULTS, **base}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            errors[key] = "Bu parametre tanınmıyor."
            continue
        values[key] = value
    for key, enum in _CHOICES.items():
        if values[key] not in {e.value for e in enum}:
            errors[key] = "Geçerli seçeneklerden biri olmalı: " + ", ".join(e.value for e in enum)
    for key, (low, high, message) in _INT_RANGES.items():
        if not _is_int(values[key]) or not low <= values[key] <= high:
            errors[key] = message
    for key in _BOOLS:
        if not isinstance(values[key], bool):
            errors[key] = "Doğru/yanlış değeri olmalı."
    days = values["reminder_days"]
    if (
        not isinstance(days, list)
        or len(days) != 3
        or not all(_is_int(d) and 1 <= d <= 120 for d in days)
        or not days[0] < days[1] < days[2]
    ):
        errors["reminder_days"] = "Üç kademe günü artan sırada, 1–120 arası olmalı."
    letter = values["demand_letter_day"]
    last = days[2] if "reminder_days" not in errors else 0
    if not _is_int(letter) or letter <= last or letter > 365:
        errors["demand_letter_day"] = (
            "İhtarname günü son hatırlatma kademesinden sonra olmalı (en fazla 365)."
        )
    rate = _decimal(values["late_fee_monthly_rate_percent"])
    if rate is None or rate != rate.quantize(Decimal("0.01")) or not 0 < rate <= LATE_FEE_RATE_MAX:
        errors["late_fee_monthly_rate_percent"] = (
            "Oran 0,01–5,00 arası olmalı (KMK m.20 aylık %5); üstü için avukat görüşü gerekir (K2)."
        )
    else:
        values["late_fee_monthly_rate_percent"] = f"{rate:.2f}"
    fee_minimum = _decimal(values["late_fee_minimum_amount"])
    if (
        fee_minimum is None
        or fee_minimum < 0
        or fee_minimum != fee_minimum.quantize(Decimal("0.01"))
    ):
        errors["late_fee_minimum_amount"] = "Tutar sıfır ya da daha büyük olmalı."
    else:
        values["late_fee_minimum_amount"] = f"{fee_minimum:.2f}"
    minimum = _decimal(values["debtor_restriction_min_amount"])
    if minimum is None or minimum < 0 or minimum != minimum.quantize(Decimal("0.01")):
        errors["debtor_restriction_min_amount"] = "Tutar sıfır ya da daha büyük olmalı."
    else:
        values["debtor_restriction_min_amount"] = f"{minimum:.2f}"
    return values, errors


def changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    merged = {**DEFAULTS, **before}
    return [k for k in DEFAULTS if merged[k] != after.get(k)]


def check_effective(effective: YearMonth, current: YearMonth, changed: list[str]) -> dict[str, str]:
    """Finansal etkili değişiklik en erken sonraki dönem; diğerleri geçmiş aya yazılamaz."""
    financial = any(k in FINANCIAL for k in changed)
    if financial and effective <= current:
        return {"effective_period": "financial"}
    if effective < current:
        return {"effective_period": "past"}
    return {}
