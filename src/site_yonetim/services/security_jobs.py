"""Güvenlik paketinin zamanlanmış işleri (Yazılım Dokümanı §3.x iş takvimi). Sunucuda cron ile:

- `run-security-daily` (günde bir, 03:00): uzun bekleyen kargo hatırlatması (sakine) ve raporu
  (yönetime); saklama süresi dolan ziyaretçi/plaka kayıtlarının anonimleştirilmesi — **yalnız
  `auto_anonymize` açık sitelerde** (varsayılan kapalı; K8 hukuk onayı bekliyor).
- `run-patrol-checks` (5 dakikada bir): başlamayan / süresi aşan devriye bildirimi.

Site başına ayrı transaction; bir sitenin hatası diğerlerini durdurmaz.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.gatehouse import months_before
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import (
    GateLog,
    ParcelDelegate,
    ParkingViolation,
    Site,
    Visitor,
    VisitorInvite,
    VisitRequest,
)
from site_yonetim.models.audit import Actor, AuditAction, record, set_actor
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import patrol as patrol_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.gatehouse import (
    load_settings,
    long_waiting,
    manager_user_ids,
    unit_user_ids,
)
from site_yonetim.services.sites import available_modules

logger = logging.getLogger(__name__)
ANONYMOUS = "Anonim"


@dataclass(frozen=True, slots=True)
class DailyResult:
    site_id: uuid.UUID
    reminded: int
    anonymized: int


async def remind_long_waiting(session: AsyncSession, site: Site, *, now: datetime) -> int:
    """Uzun bekleyen her kargo için sakine günde bir hatırlatma; yönetime tek özet."""
    days = (await load_settings(session)).data.parcel_long_wait_days
    rows = await long_waiting(session, days=days, now=now)
    today = now.astimezone(BUSINESS_TZ).date()
    sent = 0
    for item in rows:
        sent += await notify_svc.notify(
            session,
            await unit_user_ids(session, {item.package.unit_id}, today),
            notify_svc.Message(
                category=NotificationCategory.SECURITY,
                kind="package.long_waiting",
                title=f"Kargonuz {item.waiting_days} gündür bekliyor",
                body=f"{item.package.carrier or 'Kargo'} — güvenlikten teslim alın "
                "ya da teslim yetkilinizi gönderin.",
                link_entity="packages",
                link_id=item.package.id,
            ),
            dedupe_key=f"package-long-wait:{item.package.id}:{today}",
        )
    if rows:
        await notify_svc.notify(
            session,
            await manager_user_ids(session, site),
            notify_svc.Message(
                category=NotificationCategory.OPERATIONS,
                kind="package.long_waiting_report",
                title=f"{len(rows)} kargo {days} günden uzun bekliyor",
                body=", ".join(f"{r.unit_name} ({r.waiting_days} gün)" for r in rows[:10]),
            ),
            dedupe_key=f"package-long-wait-report:{today}",
        )
    return sent


async def anonymize(session: AsyncSession, *, months: int, today: date, now: datetime) -> int:
    """Saklama süresi dolan kişisel veriyi siler; kaydın kendisi (sayılar, tarih) kalır."""
    cutoff = months_before(today, months)
    cutoff_at = datetime.combine(cutoff, time.min, BUSINESS_TZ)
    statements = (
        update(Visitor)
        .where(Visitor.visit_date < cutoff, Visitor.anonymized_at.is_(None))
        .values(full_name=ANONYMOUS, phone=None, plate_number=None, note=None, anonymized_at=now),
        update(VisitorInvite)
        .where(VisitorInvite.date_to < cutoff, VisitorInvite.anonymized_at.is_(None))
        .values(visitor_name=ANONYMOUS, plate=None, note=None, anonymized_at=now),
        update(VisitRequest)
        .where(VisitRequest.visit_date < cutoff, VisitRequest.anonymized_at.is_(None))
        .values(full_name=ANONYMOUS, plate=None, anonymized_at=now),
        update(GateLog)
        .where(GateLog.log_date < cutoff, GateLog.anonymized_at.is_(None))
        .values(plate=None, detail=None, anonymized_at=now),
        update(ParkingViolation)
        .where(ParkingViolation.occurred_at < cutoff_at, ParkingViolation.anonymized_at.is_(None))
        .values(plate=None, detail=None, anonymized_at=now),
        update(ParcelDelegate)
        .where(ParcelDelegate.removed_at < cutoff_at, ParcelDelegate.name != ANONYMOUS)
        .values(name=ANONYMOUS),
    )
    total = 0
    for statement in statements:
        result = await session.execute(statement.execution_options(synchronize_session=False))
        total += int(result.rowcount or 0)  # type: ignore[attr-defined]
    if total:
        record(
            session,
            AuditAction.UPDATE,
            "security_settings",
            None,
            {"anonymized": total, "cutoff": cutoff.isoformat()},
            reason="Saklama süresi doldu (KVKK, K8)",
        )
    return total


async def _sites(factory: async_sessionmaker[AsyncSession]) -> list[Site]:
    with all_sites_scope():
        async with factory() as session:
            return list(await session.scalars(select(Site).order_by(Site.id)))


async def run_daily(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[DailyResult]:
    set_actor(Actor(None, "Güvenlik gece işi", None))
    today = now.astimezone(BUSINESS_TZ).date()
    results = []
    for site in await _sites(factory):
        try:
            with site_scope(site.id):
                async with factory() as session, session.begin():
                    modules = await available_modules(session, site)
                    reminded = anonymized = 0
                    if ModuleKey.PACKAGES in modules:
                        reminded = await remind_long_waiting(session, site, now=now)
                    settings = (await load_settings(session)).data
                    if settings.auto_anonymize:
                        anonymized = await anonymize(
                            session, months=settings.retention_months, today=today, now=now
                        )
            results.append(DailyResult(site.id, reminded, anonymized))
        except Exception:
            logger.exception("Güvenlik gece işi başarısız (site %s)", site.id)
    return results


async def run_patrol_checks(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[tuple[uuid.UUID, int, int]]:
    set_actor(Actor(None, "Devriye takibi", None))
    results = []
    for site in await _sites(factory):
        try:
            with site_scope(site.id):
                async with factory() as session, session.begin():
                    if ModuleKey.VISITORS not in await available_modules(session, site):
                        continue
                    not_started, overdue = await patrol_svc.check_site(session, site, now=now)
            if not_started or overdue:
                results.append((site.id, not_started, overdue))
        except Exception:
            logger.exception("Devriye takibi başarısız (site %s)", site.id)
    return results
