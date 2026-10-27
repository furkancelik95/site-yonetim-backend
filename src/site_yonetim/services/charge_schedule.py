"""Otomatik aylık tahakkuk — frontend servis isteği 04; Yazılım Dokümanı FIN-05; PM K21, K25.

Ayar site başına bir satır (`charge_schedules`). Gece işi (`cli run-charge-schedules`, günde bir)
açık takvimleri bulur ve her site için kendi transaction'ında:

1. Onaylanmış ve tahakkuk tarihi gelmiş taslakları kesinleştirir (FIN-05 adım 8).
2. "Tahakkuk" onay kuralı **varsa**: kesim gününden `draft_lead_days` (varsayılan 5) gün önce
   taslağı hazırlar ve ayarı açan yönetici adına onaya gönderir (`drafted`). Onaylanınca kesim
   gününde kesinleşir. Önizleme uyarıları taslakta görünür; onaycı karar verir.
3. Kural **yoksa** (bugünkü davranış): kesim günü geldiğinde elle kaydetmeyle aynı servisle keser.
   Önizlemede uyarı varsa kesmez (`skipped`); dönem kesilmişse, kaynak yoksa, borç yoksa atlar.

Vade: finans parametresi `due_day` (K25, varsayılan ayın 10'u) — sabit "+14 gün" kalktı.
Beklenmeyen hata → `failed`; ertesi gün yeniden denenir. Ay başına tek sonuç
(`charge_schedule_runs`): iş iki kez çalışırsa ikincisi bir şey yazmaz.
`notify_on_run`: bildirim sağlayıcısı yok (docs/12 K5) — sonuç yalnız kaydedilir.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.charging.schedule import charge_date_in, check_settings, next_run_on
from site_yonetim.domain.finance import FinanceRuleError, ScheduleRunStatus
from site_yonetim.models import ChargeSchedule, ChargeScheduleRun, Site
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import charge_drafts, charging
from site_yonetim.services.approvals import ApprovalError, Person

logger = logging.getLogger(__name__)

ACTOR = "Otomatik tahakkuk"
DEFAULT_DAY, DEFAULT_LEAD_DAYS = 1, 5  # hiç kaydedilmemiş ayar: kapalı, gün 1, taslak −5


# --- Ayar ---------------------------------------------------------------------------


async def get(session: AsyncSession) -> ChargeSchedule | None:
    found: ChargeSchedule | None = await session.scalar(select(ChargeSchedule))
    return found


async def save(
    session: AsyncSession,
    *,
    enabled: bool,
    charge_day: int,
    draft_lead_days: int,
    notify_on_run: bool,
    today: date,
    actor: Person | None = None,
) -> tuple[ChargeSchedule, bool]:
    """Ayarı yazar; (ayar, yeni açıldı mı). Açıldığı gün `enabled_on`'a yazılır."""
    check_settings(charge_day, draft_lead_days)
    schedule = await get(session)
    if schedule is None:
        schedule = ChargeSchedule()
        session.add(schedule)
    turned_on = enabled and not schedule.enabled
    schedule.enabled = enabled
    schedule.charge_day = charge_day
    schedule.draft_lead_days = draft_lead_days
    schedule.notify_on_run = notify_on_run
    if turned_on:
        schedule.enabled_on = today
    if enabled and actor is not None:
        schedule.enabled_by_id, schedule.enabled_by = actor.user_id, actor.name
    await session.flush()
    return schedule, turned_on


async def period_run(session: AsyncSession, period: YearMonth) -> ChargeScheduleRun | None:
    found: ChargeScheduleRun | None = await session.scalar(
        select(ChargeScheduleRun).where(
            ChargeScheduleRun.year == period.year, ChargeScheduleRun.month == period.month
        )
    )
    return found


async def month_run(session: AsyncSession, today: date) -> ChargeScheduleRun | None:
    return await period_run(session, YearMonth.of(today))


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
    """Sıradaki kesim günü (taslak bundan `draft_lead_days` gün önce hazırlanır)."""
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


async def _post_directly(
    session: AsyncSession, charge_date: date, today: date, now: datetime
) -> Outcome:
    try:
        async with session.begin_nested():  # yarım kalan yazım geri alınsın, sonuç yine yazılsın
            prepared = await charging.prepare(session, today, charge_date=charge_date)
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


async def _draft(
    session: AsyncSession,
    site: Site,
    schedule: ChargeSchedule,
    charge_date: date,
    today: date,
    now: datetime,
) -> Outcome:
    try:
        async with session.begin_nested():
            run, prepared = await charge_drafts.create(
                session, today, created_by=ACTOR, charge_date=charge_date
            )
            if schedule.enabled_by_id is None:
                return Outcome(
                    ScheduleRunStatus.DRAFTED,
                    f"{prepared.period.name} taslağı hazırlandı; onaya göndermek için tahakkuk "
                    "ekranından gönderin.",
                    run.id,
                )
            requester = Person(schedule.enabled_by_id, f"{ACTOR} ({schedule.enabled_by})")
            submitted = await charge_drafts.submit(
                session, site, run, actor=requester, today=today, now=now
            )
    except (FinanceRuleError, ApprovalError) as exc:
        return Outcome(ScheduleRunStatus.SKIPPED, exc.message)
    except Exception:
        logger.exception("Otomatik tahakkuk taslağı başarısız")
        return Outcome(ScheduleRunStatus.FAILED, "Beklenmeyen hata; yarın yeniden denenecek.")
    if submitted.posted is not None:  # kural arada kaldırıldıysa hemen kesinleşir
        return Outcome(ScheduleRunStatus.POSTED, None, run.id)
    number = submitted.request.number if submitted.request else None
    return Outcome(
        ScheduleRunStatus.DRAFTED,
        f"{prepared.period.name} taslağı hazırlandı ve onaya gönderildi (talep #{number}); "
        "onaylanınca kesim gününde kesinleşir.",
        run.id,
    )


async def _record(
    session: AsyncSession, period: YearMonth, outcome: Outcome, now: datetime
) -> None:
    row = await period_run(session, period) or ChargeScheduleRun(
        year=period.year, month=period.month
    )
    row.status = outcome.status.value
    row.message = outcome.message
    row.charge_run_id = outcome.charge_run_id
    row.ran_at = now
    session.add(row)
    await session.flush()


async def finalize_approved(session: AsyncSession, today: date, now: datetime) -> list[Outcome]:
    """Onaylı ve tarihi gelmiş taslakları kesinleştirir; sonucu ay satırına işler."""
    outcomes = []
    for posted in await charge_drafts.finalize_due(session, today, now):
        outcome = Outcome(ScheduleRunStatus.POSTED, None, posted.run.id)
        await _record(session, posted.period, outcome, now)
        outcomes.append(outcome)
    return outcomes


async def run_site(
    session: AsyncSession, site: Site, schedule: ChargeSchedule, *, today: date, now: datetime
) -> list[Outcome]:
    """Sitenin bugünkü otomatik tahakkuk işleri. Boş liste: yapılacak bir şey yok."""
    outcomes = await finalize_approved(session, today, now)
    if not schedule.enabled or schedule.enabled_on is None:
        return outcomes
    with_rule = await charge_drafts.rule_exists(session, today)
    this_month = YearMonth.of(today)
    for period in (this_month, this_month.next()):
        charge_date = charge_date_in(period.year, period.month, schedule.charge_day)
        if charge_date < schedule.enabled_on:
            continue  # açıldığı günden önceki kesim günleri geriye dönük kesilmez
        start = charge_date - timedelta(days=schedule.draft_lead_days) if with_rule else charge_date
        if start > today:
            continue
        existing = await period_run(session, period)
        if _settled(existing):
            if period == this_month and not with_rule and not outcomes:
                outcomes.append(
                    Outcome(
                        ScheduleRunStatus.SKIPPED,
                        f"{period.name} için otomatik tahakkuk zaten çalıştı.",
                    )
                )
            continue
        outcome = (
            await _draft(session, site, schedule, charge_date, today, now)
            if with_rule
            else await _post_directly(session, charge_date, today, now)
        )
        await _record(session, period, outcome, now)
        outcomes.append(outcome)
    return outcomes


@dataclass(frozen=True, slots=True)
class SiteResult:
    site_id: uuid.UUID
    outcome: Outcome


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[SiteResult]:
    """Açık takvimi ya da onaylı taslağı olan her site için, site başına ayrı transaction."""
    from site_yonetim.domain.finance import ChargeRunStatus
    from site_yonetim.models import ChargeRun

    with all_sites_scope():
        async with factory() as session:
            site_ids = sorted(
                set(
                    await session.scalars(
                        select(ChargeSchedule.site_id).where(ChargeSchedule.enabled)
                    )
                )
                | set(
                    await session.scalars(
                        select(ChargeRun.site_id).where(
                            ChargeRun.status == ChargeRunStatus.APPROVED.value
                        )
                    )
                )
            )
    set_actor(Actor(None, ACTOR, None))
    results: list[SiteResult] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                schedule = await get(session) or ChargeSchedule(enabled=False)
                outcomes = (
                    await run_site(session, site, schedule, today=today, now=now) if site else []
                )
        results.extend(SiteResult(site_id, outcome) for outcome in outcomes)
    return results
