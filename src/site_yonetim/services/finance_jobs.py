"""Finans Mod P gece/saatlik işi (`cli run-finance-jobs`): site başına ayrı transaction.

1. Süresi dolan süreli açmaları yeniden kilitler (FIN-13).
2. Borç takibi kademe kayıtları (FIN-12; gönderim K5'e bağlı — yalnız kayıt + yöneticiye görev).
3. Bozulan ödeme planlarını işaretler ve finans yetkililerine bir kez bildirir (FIN-12).
4. Gecikme tazminatı açıksa (K2) önceki ayın ay sonu gecikme kaydını yazar — dönem başına bir kez
   (FIN-03, docs/04 §6; `services/late_fees.py`).
5. Bütçe sapma uyarısı: kategori %90 / %100 eşiği (FIN-17; `services/annual_budgets.py`).
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
from site_yonetim.services import (
    annual_budgets,
    debt_tracking,
    late_fees,
    payment_plans,
    periods,
)

logger = logging.getLogger(__name__)
JOB_ACTOR = "Finans işi"


@dataclass(frozen=True, slots=True)
class SiteResult:
    relocked: int
    reminders: int
    broken_plans: int
    late_fee_accounts: int = 0
    budget_alerts: int = 0


async def run_site(session: AsyncSession, site: Site, *, now: datetime, today: date) -> SiteResult:
    relocked = await periods.relock_expired(session, now)
    reminders = await debt_tracking.run_stages(session, site, today)
    broken = await payment_plans.mark_broken(session, site, today, now)
    late = await late_fees.run_month_end(session, today, now)
    alerts = await annual_budgets.run_alerts(session, site, today)
    return SiteResult(relocked, reminders, broken, late.account_count if late else 0, alerts)


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
