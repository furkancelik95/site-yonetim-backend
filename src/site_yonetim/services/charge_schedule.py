"""Otomatik aylık tahakkuk — frontend servis isteği 04.

Ayar site başına bir satır (`charge_schedules`). Gece işi (`cli run-charge-schedules`, günde bir)
açık takvimleri bulur; kesim günü gelen her site için **elle kaydetmeyle aynı servisi**
(`charging.prepare` + `charging.post`) kendi transaction'ında çalıştırır:

- Dönem zaten kesilmişse, kesinleşmiş proje yoksa, kesilecek borç yoksa → `skipped` + mesaj.
- Önizlemede uyarı varsa (ödeyen yok, ağırlık verisi eksik…) **kesilmez** → `skipped` + uyarı;
  yönetici elle bakar (servis isteğindeki öneri).
- Beklenmeyen hata → `failed`; ertesi gün yeniden denenir.
- Ay başına tek sonuç (`charge_schedule_runs`): iş iki kez çalışırsa ikincisi bir şey yazmaz.
- `notify_on_run`: bildirim sağlayıcısı yok (docs/12 K5) — sonuç yalnız kaydedilir.

Tahakkuk kaydı denetim kaydına "Otomatik tahakkuk" aktörüyle düşer.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.charging.schedule import (
    charge_date_in,
    check_settings,
    is_due,
    next_run_on,
)
from site_yonetim.domain.finance import FinanceRuleError, ScheduleRunStatus
from site_yonetim.models import ChargeSchedule, ChargeScheduleRun
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import charging

logger = logging.getLogger(__name__)

ACTOR = "Otomatik tahakkuk"
DEFAULT_DAY, DEFAULT_DUE_DAYS = 1, 14  # hiç kaydedilmemiş ayar: kapalı, gün 1, vade 14


# --- Ayar ---------------------------------------------------------------------------


async def get(session: AsyncSession) -> ChargeSchedule | None:
    found: ChargeSchedule | None = await session.scalar(select(ChargeSchedule))
    return found


async def save(
    session: AsyncSession,
    *,
    enabled: bool,
    charge_day: int,
    due_days: int,
    notify_on_run: bool,
    today: date,
) -> tuple[ChargeSchedule, bool]:
    """Ayarı yazar; (ayar, yeni açıldı mı). Açıldığı gün `enabled_on`'a yazılır."""
    check_settings(charge_day, due_days)
    schedule = await get(session)
    if schedule is None:
        schedule = ChargeSchedule()
        session.add(schedule)
    turned_on = enabled and not schedule.enabled
    schedule.enabled = enabled
    schedule.charge_day = charge_day
    schedule.due_days = due_days
    schedule.notify_on_run = notify_on_run
    if turned_on:
        schedule.enabled_on = today
    await session.flush()
    return schedule, turned_on


async def month_run(session: AsyncSession, today: date) -> ChargeScheduleRun | None:
    found: ChargeScheduleRun | None = await session.scalar(
        select(ChargeScheduleRun).where(
            ChargeScheduleRun.year == today.year, ChargeScheduleRun.month == today.month
        )
    )
    return found


async def last_run(session: AsyncSession) -> ChargeScheduleRun | None:
    found: ChargeScheduleRun | None = await session.scalar(
        select(ChargeScheduleRun)
        .order_by(ChargeScheduleRun.year.desc(), ChargeScheduleRun.month.desc())
        .limit(1)
    )
    return found


def _settled(run: ChargeScheduleRun | None) -> bool:
    return run is not None and run.status != ScheduleRunStatus.FAILED.value


async def next_run(session: AsyncSession, schedule: ChargeSchedule, today: date) -> date | None:
    if not schedule.enabled or schedule.enabled_on is None:
        return None
    settled = _settled(await month_run(session, today))
    return next_run_on(today, schedule.charge_day, schedule.enabled_on, settled=settled)


# --- Gece işi -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    status: ScheduleRunStatus
    message: str | None
    charge_run_id: uuid.UUID | None = None


def _warning_text(warnings: list[str]) -> str:
    head = f"Önizlemede {len(warnings)} uyarı var; tahakkuk kesilmedi, elle kontrol edin: "
    return head + warnings[0] + (" …" if len(warnings) > 1 else "")


async def _attempt(
    session: AsyncSession, schedule: ChargeSchedule, today: date, now: datetime
) -> Outcome:
    charge_date = charge_date_in(today.year, today.month, schedule.charge_day)
    try:
        async with session.begin_nested():  # yarım kalan yazım geri alınsın, sonuç yine yazılsın
            prepared = await charging.prepare(
                session,
                today,
                charge_date=charge_date,
                due_date=charge_date + timedelta(days=schedule.due_days),
            )
            if prepared.preview.warnings:
                return Outcome(
                    ScheduleRunStatus.SKIPPED,
                    _warning_text([w.message for w in prepared.preview.warnings]),
                )
            posted = await charging.post(session, prepared, posted_by=ACTOR, now=now)
    except FinanceRuleError as exc:
        return Outcome(ScheduleRunStatus.SKIPPED, exc.message)
    except Exception:
        logger.exception("Otomatik tahakkuk başarısız")
        return Outcome(ScheduleRunStatus.FAILED, "Beklenmeyen hata; yarın yeniden denenecek.")
    return Outcome(ScheduleRunStatus.POSTED, None, posted.run.id)


async def run_site(
    session: AsyncSession, schedule: ChargeSchedule, *, today: date, now: datetime
) -> Outcome | None:
    """Sitenin bu ayki otomatik tahakkuku. `None`: bugün yapılacak bir şey yok."""
    if not schedule.enabled or schedule.enabled_on is None:
        return None
    if not is_due(today, schedule.charge_day, schedule.enabled_on):
        return None
    existing = await month_run(session, today)
    if _settled(existing):
        period = YearMonth(today.year, today.month).name
        return Outcome(ScheduleRunStatus.SKIPPED, f"{period} için otomatik tahakkuk zaten çalıştı.")
    outcome = await _attempt(session, schedule, today, now)
    row = existing or ChargeScheduleRun(year=today.year, month=today.month)
    row.status = outcome.status.value
    row.message = outcome.message
    row.charge_run_id = outcome.charge_run_id
    row.ran_at = now
    session.add(row)
    await session.flush()
    return outcome


@dataclass(frozen=True, slots=True)
class SiteResult:
    site_id: uuid.UUID
    outcome: Outcome


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[SiteResult]:
    """Açık takvimi olan her site için, site başına ayrı transaction."""
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(
                await session.scalars(
                    select(ChargeSchedule.site_id)
                    .where(ChargeSchedule.enabled)
                    .order_by(ChargeSchedule.site_id)
                )
            )
    set_actor(Actor(None, ACTOR, None))
    results = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                schedule = await get(session)
                outcome = (
                    await run_site(session, schedule, today=today, now=now) if schedule else None
                )
        if outcome is not None:
            results.append(SiteResult(site_id, outcome))
    return results
