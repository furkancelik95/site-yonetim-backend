"""Otomatik aylık tahakkuk takvimi — saf kurallar (frontend servis isteği 04).

Saat parametre olarak gelir (domain saati okumaz); tarihler İstanbul yerel tarihidir.

- Kesim günü 1–28: her ayda bulunsun diye.
- Ay içinde kesim günü geldiyse (ya da geçtiyse) ve o ay henüz sonuçlanmadıysa iş keser —
  sunucu kesim günü kapalıysa ertesi gün yetişir.
- Otomatik tahakkuk **açıldığı günden önceki** kesim günleri için geriye dönük kesmez: ayın
  5'inde açılan ve günü 1 olan takvim o ayı değil, sonraki ayı keser.
"""

from datetime import date

from site_yonetim.domain.finance import FinanceRuleError

MIN_DAY, MAX_DAY = 1, 28
MIN_DUE, MAX_DUE = 0, 60


def check_settings(charge_day: int, due_days: int) -> None:
    if not MIN_DAY <= charge_day <= MAX_DAY:
        raise FinanceRuleError(
            "invalid_charge_day",
            "Gün 1–28 arasında olmalı (her ayda olsun diye).",
            field="charge_day",
        )
    if not MIN_DUE <= due_days <= MAX_DUE:
        raise FinanceRuleError("invalid_due_days", "Vade 0–60 gün olmalı.", field="due_days")


def charge_date_in(year: int, month: int, charge_day: int) -> date:
    return date(year, month, charge_day)


def _next_month(day: date) -> tuple[int, int]:
    return (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)


def is_due(today: date, charge_day: int, enabled_on: date) -> bool:
    """Bu ayın kesimi yapılmalı mı (henüz sonuçlanmadıysa)?"""
    this_month = charge_date_in(today.year, today.month, charge_day)
    return enabled_on <= this_month <= today


def next_run_on(today: date, charge_day: int, enabled_on: date, *, settled: bool) -> date:
    """Sıradaki kesim günü. `settled`: bu ayın sonucu (kesildi/atlandı) zaten var.

    next_run_on(date(2026, 10, 2), 1, date(2026, 9, 1), settled=True) → 2026-11-01
    """
    this_month = charge_date_in(today.year, today.month, charge_day)
    if not settled and enabled_on <= this_month:
        return max(this_month, today)  # bekleyen (geçtiyse sıradaki çalışmada)
    year, month = _next_month(today)
    return charge_date_in(year, month, charge_day)
