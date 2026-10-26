"""Planlı rapor gönderimi — Yazılım Dokümanı RAP-05, §4.6.

- Tanım: rapor türü, periyot (günlük/haftalık/aylık), gönderim saati (tesis saati), alıcılar,
  format. Alıcılar raporun verisine yetkili site kullanıcıları olmalı (RAP-05 adım 2 —
  yetkisiz alıcı eklenemez); çalıştırma anında da yeniden süzülür (rolü değişen alıcıya gitmez).
- İş (`cli.py run-scheduled-reports`, dakikalık/saatlik): zamanı gelen tanım için e-posta
  üretir ve gönderim katmanıyla (`services/delivery.py`) yollar; çalışma kaydı yazılır, sonraki
  zaman hesaplanır. Aynı zaman dilimi iki kez çalışmaz (benzersiz kısıt).
- Rapor içerikleri sonra gelir: e-posta şimdilik rapor adı, dönem ve panel bağlantısını taşır.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import ROLE_PERMISSIONS
from site_yonetim.domain.notifications import NotificationCategory, NotificationChannel
from site_yonetim.domain.scheduled_reports import (
    FREQUENCY_LABELS,
    MAX_RECIPIENTS,
    REPORT_KINDS,
    ReportFormat,
    ReportFrequency,
    ReportRunStatus,
    ScheduleError,
    check_schedule,
    next_run,
    period_label,
)
from site_yonetim.models import ScheduledReport, ScheduledReportRun, Site
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import delivery
from site_yonetim.services import members as members_svc

JOB_ACTOR = "Planlı rapor işi"


class ReportRuleError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: uuid.UUID
    full_name: str
    email: str | None


async def eligible_recipients(
    session: AsyncSession, site: Site, report_key: str
) -> dict[uuid.UUID, Recipient]:
    """Raporun verisine yetkili, etkin site kullanıcıları (e-postası olanlar gönderim alır)."""
    kind = REPORT_KINDS[report_key]
    return {
        m.user.id: Recipient(m.user.id, m.user.full_name, m.user.email)
        for m in await members_svc.list_members(session, site)
        if m.is_active
        and m.user.is_active
        and kind.permission in {p.value for p in ROLE_PERMISSIONS[m.role]}
    }


@dataclass(frozen=True, slots=True)
class ReportData:
    name: str
    report_key: str
    frequency: ReportFrequency
    weekday: int | None
    day_of_month: int | None
    send_time: time
    format: ReportFormat
    recipient_user_ids: list[uuid.UUID]
    is_active: bool = True


async def _validate(session: AsyncSession, site: Site, data: ReportData) -> None:
    errors: dict[str, str] = {}
    name = data.name.strip()
    if not 2 <= len(name) <= 120:
        errors["name"] = "Rapor adı 2–120 karakter olmalı."
    if data.report_key not in REPORT_KINDS:
        errors["report_key"] = "Rapor seçin."
    try:
        check_schedule(data.frequency, data.weekday, data.day_of_month)
    except ScheduleError as exc:
        errors[exc.field] = str(exc)
    ids = list(dict.fromkeys(data.recipient_user_ids))
    if not ids:
        errors["recipient_user_ids"] = "En az bir alıcı seçin."
    elif len(ids) > MAX_RECIPIENTS:
        errors["recipient_user_ids"] = f"En fazla {MAX_RECIPIENTS} alıcı seçilebilir."
    elif data.report_key in REPORT_KINDS:
        allowed = await eligible_recipients(session, site, data.report_key)
        if any(i not in allowed for i in ids):
            errors["recipient_user_ids"] = (
                "Raporun verisine yetkisi olmayan alıcı eklenemez (RAP-05)."
            )
        elif not any(allowed[i].email for i in ids):
            errors["recipient_user_ids"] = "Seçilen alıcıların hiçbirinin e-postası yok."
    if errors:
        raise ReportRuleError(errors)


async def create(
    session: AsyncSession, site: Site, data: ReportData, *, now: datetime, by: str
) -> ScheduledReport:
    await _validate(session, site, data)
    report = ScheduledReport(
        name=data.name.strip(),
        report_key=data.report_key,
        frequency=data.frequency.value,
        weekday=data.weekday if data.frequency is ReportFrequency.WEEKLY else None,
        day_of_month=data.day_of_month if data.frequency is ReportFrequency.MONTHLY else None,
        send_time=data.send_time,
        format=data.format.value,
        recipient_user_ids=list(dict.fromkeys(data.recipient_user_ids)),
        is_active=data.is_active,
        next_run_at=_next(data, now),
        created_by_name=by,
    )
    session.add(report)
    await session.flush()
    return report


def _next(data: ReportData, after: datetime) -> datetime:
    return next_run(
        data.frequency,
        send_time=data.send_time,
        weekday=data.weekday,
        day_of_month=data.day_of_month,
        after=after,
        tz=delivery.BUSINESS_TZ,
    )


async def update(
    session: AsyncSession, site: Site, report: ScheduledReport, data: ReportData, *, now: datetime
) -> ScheduledReport:
    await _validate(session, site, data)
    report.name = data.name.strip()
    report.report_key = data.report_key
    report.frequency = data.frequency.value
    report.weekday = data.weekday if data.frequency is ReportFrequency.WEEKLY else None
    report.day_of_month = data.day_of_month if data.frequency is ReportFrequency.MONTHLY else None
    report.send_time = data.send_time
    report.format = data.format.value
    report.recipient_user_ids = list(dict.fromkeys(data.recipient_user_ids))
    report.is_active = data.is_active
    report.next_run_at = _next(data, now)
    await session.flush()
    return report


def data_of(report: ScheduledReport) -> ReportData:
    return ReportData(
        name=report.name,
        report_key=report.report_key,
        frequency=ReportFrequency(report.frequency),
        weekday=report.weekday,
        day_of_month=report.day_of_month,
        send_time=report.send_time,
        format=ReportFormat(report.format),
        recipient_user_ids=list(report.recipient_user_ids),
        is_active=report.is_active,
    )


async def run_one(
    session: AsyncSession, site: Site, report: ScheduledReport, *, run_at: datetime, web_url: str
) -> ScheduledReportRun:
    """Tek çalıştırma: yetkili alıcılara e-posta kuyruğa alınır, çalışma kaydı yazılır."""
    frequency = ReportFrequency(report.frequency)
    label = period_label(frequency, run_at, delivery.BUSINESS_TZ)
    allowed = await eligible_recipients(session, site, report.report_key)
    targets = [allowed[i] for i in report.recipient_user_ids if i in allowed and allowed[i].email]
    kind = REPORT_KINDS[report.report_key]
    run = ScheduledReportRun(
        report_id=report.id,
        run_at=run_at,
        period_label=label,
        status=ReportRunStatus.SENT.value if targets else ReportRunStatus.SKIPPED.value,
        recipient_count=len(targets),
        error=None if targets else "Yetkili ve e-postası olan alıcı kalmadı.",
    )
    session.add(run)
    for t in targets:
        await delivery.queue_direct(
            session,
            channel=NotificationChannel.EMAIL,
            to=t.email or "",
            subject=f"{site.name} · {report.name} ({label})",
            body=(
                f"Sayın {t.full_name},\n\n{kind.label} — {FREQUENCY_LABELS[frequency].lower()} "
                f"planlı rapor, dönem: {label}. Format: {report.format.upper()}.\n"
                f"Raporu panelden açın: {web_url}/s/{site.slug}/rapor-katalogu\n\n"
                "Bu e-posta planlı rapor tanımı nedeniyle gönderildi."
            ),
            category=NotificationCategory.OPERATIONS,
            kind=f"report.{report.report_key}",
            user_id=t.user_id,
            now=run_at,
        )
    report.last_run_at = run_at
    await session.flush()
    return run


async def run_due(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, web_url: str
) -> list[tuple[uuid.UUID, int]]:
    """Zamanı gelen planlı raporlar (site başına ayrı transaction; tekrar güvenli)."""
    due = select(ScheduledReport).where(
        ScheduledReport.is_active, ScheduledReport.next_run_at <= now
    )
    with all_sites_scope():
        async with factory() as session:
            site_ids = sorted(
                set(await session.scalars(due.with_only_columns(ScheduledReport.site_id)))
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, int]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                count = 0
                for report in list(await session.scalars(due.with_for_update(skip_locked=True))):
                    await run_one(session, site, report, run_at=report.next_run_at, web_url=web_url)
                    report.next_run_at = _next(data_of(report), max(now, report.next_run_at))
                    count += 1
                results.append((site_id, count))
    return results
