"""Planlı rapor tanımı — saf (Yazılım Dokümanı RAP-05, §4.6 "Planlı rapor gönderimi").

Tanım: rapor, periyot (günlük / haftalık / aylık), gönderim saati, alıcılar, format. Alıcılar
raporun verisine yetkili site kullanıcıları olmalı (adım 2 — yetkisiz alıcı eklenemez). Rapor
içerikleri sonra gelir; şimdilik iş tanıma göre zamanı gelince e-postayı üretir ve gönderir.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class ReportFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ReportFormat(StrEnum):
    PDF = "pdf"
    XLSX = "xlsx"


class ReportRunStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # yetkili alıcı kalmadı


@dataclass(frozen=True, slots=True)
class ReportKind:
    key: str
    label: str
    permission: str  # alıcıda bulunması gereken izin (domain/access.Permission değeri)


REPORT_KINDS: dict[str, ReportKind] = {
    k.key: k
    for k in (
        ReportKind("income_expense", "Gelir–gider raporu", "finance.reports.read"),
        ReportKind("collections", "Aidat tahsilat özeti", "finance.reports.read"),
        ReportKind("debtors", "Borçlu listesi", "finance.read"),
        ReportKind("cash", "Kasa ve banka özeti", "finance.cash.read"),
        ReportKind("requests_sla", "Talep ve SLA raporu", "requests.read"),
    )
}

FREQUENCY_LABELS = {
    ReportFrequency.DAILY: "Günlük",
    ReportFrequency.WEEKLY: "Haftalık",
    ReportFrequency.MONTHLY: "Aylık",
}
MAX_RECIPIENTS = 20


class ScheduleError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def check_schedule(
    frequency: ReportFrequency, weekday: int | None, day_of_month: int | None
) -> None:
    if frequency is ReportFrequency.WEEKLY and (weekday is None or not 0 <= weekday <= 6):
        raise ScheduleError("weekday", "Haftalık raporda gönderim gününü seçin.")
    if frequency is ReportFrequency.MONTHLY and (
        day_of_month is None or not 1 <= day_of_month <= 28
    ):
        raise ScheduleError("day_of_month", "Aylık raporda 1–28 arasında bir gün seçin.")


def next_run(
    frequency: ReportFrequency,
    *,
    send_time: time,
    weekday: int | None,
    day_of_month: int | None,
    after: datetime,
    tz: ZoneInfo,
) -> datetime:
    """`after`tan sonraki ilk gönderim anı (UTC). Saat tesis saat diliminde."""
    local = after.astimezone(tz)
    day = local.date()
    for _ in range(400):
        if _matches(frequency, day, weekday, day_of_month):
            candidate = datetime.combine(day, send_time, tzinfo=tz)
            if candidate > local:
                return candidate.astimezone(UTC)
        day += timedelta(days=1)
    raise ScheduleError("frequency", "Gönderim zamanı hesaplanamadı.")  # pragma: no cover


def _matches(
    frequency: ReportFrequency, day: date, weekday: int | None, day_of_month: int | None
) -> bool:
    if frequency is ReportFrequency.DAILY:
        return True
    if frequency is ReportFrequency.WEEKLY:
        return day.weekday() == weekday
    last = calendar.monthrange(day.year, day.month)[1]
    return day.day == min(day_of_month or 1, last)


def period_label(frequency: ReportFrequency, run_at: datetime, tz: ZoneInfo) -> str:
    """Raporun kapsadığı dönem: önceki gün / önceki 7 gün / önceki ay."""
    local = run_at.astimezone(tz).date()
    if frequency is ReportFrequency.DAILY:
        return (local - timedelta(days=1)).strftime("%d.%m.%Y")
    if frequency is ReportFrequency.WEEKLY:
        start = local - timedelta(days=7)
        end = local - timedelta(days=1)
        return f"{start:%d.%m.%Y} – {end:%d.%m.%Y}"
    prev = local.replace(day=1) - timedelta(days=1)
    return f"{prev:%m.%Y}"
