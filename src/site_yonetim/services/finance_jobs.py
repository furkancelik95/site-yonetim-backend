"""Finans Mod P gece/saatlik işi (`cli run-finance-jobs`): site başına ayrı transaction.

1. Süresi dolan süreli açmaları yeniden kilitler (FIN-13).
2. Borç takibi kademe kayıtları (FIN-12; gönderim K5'e bağlı — yalnız kayıt + yöneticiye görev).
3. Bozulan ödeme planlarını işaretler ve finans yetkililerine bir kez bildirir (FIN-12).
Tekrar güvenli: aynı kademe/bildirim ikinci kez yazılmaz; bir sitedeki hata diğerini durdurmaz.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.models import Site
from site_yonetim.models.audit import Actor, ActorType, set_actor
from site_yonetim.services import debt_tracking, payment_plans, periods

logger = logging.getLogger(__name__)
JOB_ACTOR = "Finans işi"


@dataclass(frozen=True, slots=True)
class SiteResult:
    relocked: int
    reminders: int
    broken_plans: int


async def run_site(session: AsyncSession, site: Site, *, now: datetime, today: date) -> SiteResult:
    relocked = await periods.relock_expired(session, now)
    reminders = await debt_tracking.run_stages(session, site, today)
    broken = await payment_plans.mark_broken(session, site, today, now)
    return SiteResult(relocked, reminders, broken)


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, today: date
) -> list[tuple[uuid.UUID, SiteResult]]:
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(await session.scalars(select(Site.id).order_by(Site.created_at)))
    set_actor(Actor(None, JOB_ACTOR, None, kind=ActorType.SYSTEM))
    results: list[tuple[uuid.UUID, SiteResult]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            try:
                async with factory() as session, session.begin():
                    site = await session.get(Site, site_id)
                    if site is None:  # pragma: no cover
                        continue
                    results.append((site_id, await run_site(session, site, now=now, today=today)))
            except Exception:  # pragma: no cover - bir site diğerlerini durdurmaz
                logger.exception("Finans işi %s sitesinde başarısız", site_id)
    return results
