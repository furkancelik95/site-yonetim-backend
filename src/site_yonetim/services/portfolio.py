"""Portföy ve platform özeti — docs/06 §2.2–§2.3, docs/05 §5. Salt okunur.

İkisi de **bilinçli "tüm siteler" kapsamı** kullanır (docs/02 §3, docs/09 §1) — ayrı bir
oturumda ve her sorgu açıkça izin verilen site kümesiyle süzülür. Sorgu sayısı site sayısından
bağımsızdır (site başına döngüde sorgu yok, docs/08 §3).

- Portföy: kullanıcının eriştiği siteler; finans rakamları yalnız `finance.read` olan,
  talepler yalnız `requests.read` olan sitelerde.
- Platform özeti: yalnız kullanım ölçüsü (müşteri, site, bölüm, tavan) — borç/sakin verisi yok.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.money import EPSILON, ZERO
from site_yonetim.domain.operations import RequestStatus
from site_yonetim.domain.reports import percent
from site_yonetim.models import (
    AccountBalance,
    Organization,
    Plan,
    Request,
    Site,
    SiteFinanceSummary,
    Unit,
)

OPEN = [RequestStatus.OPEN.value, RequestStatus.IN_PROGRESS.value, RequestStatus.WAITING.value]


async def unit_counts(
    session: AsyncSession, site_ids: Collection[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    query = select(Unit.site_id, func.count()).where(Unit.is_active).group_by(Unit.site_id)
    if site_ids is not None:
        query = query.where(Unit.site_id.in_(list(site_ids)))
    return dict((await session.execute(query)).all())


# --- Portföy ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteFinance:
    charged: Decimal
    collected: Decimal
    open_balance: Decimal
    debtor_count: int

    @property
    def collection_rate(self) -> Decimal:
        return percent(self.collected, self.charged)


@dataclass(frozen=True, slots=True)
class SiteRequests:
    open: int
    overdue: int  # vadesi (due_at) geçmiş açık talepler


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    site_id: uuid.UUID
    units: int
    finance: SiteFinance | None
    requests: SiteRequests | None


async def portfolio(
    factory: async_sessionmaker[AsyncSession],
    *,
    site_ids: Collection[uuid.UUID],
    finance_sites: Collection[uuid.UUID],
    request_sites: Collection[uuid.UUID],
    now: datetime,
) -> list[PortfolioRow]:
    ids = list(site_ids)
    with all_sites_scope():
        async with factory() as session:
            units = await unit_counts(session, ids)
            totals = {
                site_id: (charged, collected)
                for site_id, charged, collected in await session.execute(
                    select(
                        SiteFinanceSummary.site_id,
                        func.sum(SiteFinanceSummary.charged),
                        func.sum(SiteFinanceSummary.collected),
                    )
                    .where(SiteFinanceSummary.site_id.in_(list(finance_sites)))
                    .group_by(SiteFinanceSummary.site_id)
                )
            }
            debt = {
                site_id: (total, count)
                for site_id, total, count in await session.execute(
                    select(AccountBalance.site_id, func.sum(AccountBalance.balance), func.count())
                    .where(
                        AccountBalance.site_id.in_(list(finance_sites)),
                        AccountBalance.balance > EPSILON,
                    )
                    .group_by(AccountBalance.site_id)
                )
            }
            overdue = and_(Request.due_at.is_not(None), Request.due_at < now)
            requests = {
                site_id: (open_count, late)
                for site_id, open_count, late in await session.execute(
                    select(Request.site_id, func.count(), func.count(case((overdue, 1))))
                    .where(Request.site_id.in_(list(request_sites)), Request.status.in_(OPEN))
                    .group_by(Request.site_id)
                )
            }
    finance_set, request_set = set(finance_sites), set(request_sites)
    rows = []
    for site_id in ids:
        finance = None
        if site_id in finance_set:
            charged, collected = totals.get(site_id, (ZERO, ZERO))
            open_balance, debtors = debt.get(site_id, (ZERO, 0))
            finance = SiteFinance(
                Decimal(charged or 0), Decimal(collected or 0), Decimal(open_balance or 0), debtors
            )
        site_requests = None
        if site_id in request_set:
            open_count, late = requests.get(site_id, (0, 0))
            site_requests = SiteRequests(open_count, late)
        rows.append(PortfolioRow(site_id, units.get(site_id, 0), finance, site_requests))
    return rows


# --- Platform özeti ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteUsage:
    site: Site
    organization_name: str | None
    plan_name: str | None
    max_units: int | None
    units: int

    @property
    def over_cap(self) -> bool:
        return self.max_units is not None and self.units > self.max_units


@dataclass(frozen=True, slots=True)
class CustomerUsage:
    organization: Organization
    plan_name: str | None
    site_count: int
    units: int


@dataclass(frozen=True, slots=True)
class Overview:
    customer_count: int
    site_count: int
    unit_count: int
    sites: list[SiteUsage]
    customers: list[CustomerUsage]

    @property
    def over_cap(self) -> list[SiteUsage]:
        return [s for s in self.sites if s.over_cap]


async def overview(factory: async_sessionmaker[AsyncSession]) -> Overview:
    """Kullanım ölçüsü; site ve müşteri listeleri ada göre sıralı (platform ölçeğinde küçük)."""
    with all_sites_scope():
        async with factory() as session:
            units = await unit_counts(session)
            sites = list(
                await session.execute(
                    select(Site, Organization.name, Plan.name, Plan.max_units)
                    .outerjoin(Organization, Organization.id == Site.organization_id)
                    .outerjoin(Plan, Plan.id == Site.plan_id)
                    .order_by(Site.name)
                )
            )
            organizations = list(
                await session.execute(
                    select(Organization, Plan.name)
                    .outerjoin(Plan, Plan.id == Organization.plan_id)
                    .order_by(Organization.name)
                )
            )
    usage = [
        SiteUsage(site, org_name, plan_name, max_units, units.get(site.id, 0))
        for site, org_name, plan_name, max_units in sites
    ]
    per_org: dict[uuid.UUID, list[SiteUsage]] = {}
    for item in usage:
        if item.site.organization_id is not None:
            per_org.setdefault(item.site.organization_id, []).append(item)
    customers = [
        CustomerUsage(
            org,
            plan_name,
            len(per_org.get(org.id, [])),
            sum(s.units for s in per_org.get(org.id, [])),
        )
        for org, plan_name in organizations
    ]
    return Overview(
        customer_count=len(customers),
        site_count=len(usage),
        unit_count=sum(units.values()),
        sites=usage,
        customers=customers,
    )
