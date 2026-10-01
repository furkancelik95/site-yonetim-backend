"""Portföy — docs/06 §2.3. Birden çok siteye erişen kullanıcı (yönetim şirketi) görür; tek
siteli kullanıcı ve platform yöneticisi için uç yoktur (404).

Site başına finans rakamları yalnız o sitede `finance.read`, talep sayıları yalnız
`requests.read` varsa dolar; yoksa `null`. "Sağlık durumu" tanımı açık karar (docs/12 K19).
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from site_yonetim.api.deps import FactoryDep, NowDep, UserAccessDep
from site_yonetim.api.schemas import Money
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.reports import percent
from site_yonetim.services import portfolio as svc

router = APIRouter(tags=["portföy"])


class SiteFinanceOut(BaseModel):
    charged: Money = Field(description="kesilen tahakkuk (toplam)")
    collected: Money = Field(description="tahsil edilen (toplam)")
    collection_rate: str = Field(description="tahsilat oranı %, metin")
    open_balance: Money
    debtor_count: int


class SiteRequestsOut(BaseModel):
    open: int
    overdue: int = Field(description="hedef tarihi geçmiş açık talepler")


class PortfolioSite(BaseModel):
    site_id: uuid.UUID
    name: str
    slug: str
    role: str
    units: int
    finance: SiteFinanceOut | None = Field(description="finance.read yoksa null")
    requests: SiteRequestsOut | None = Field(description="requests.read yoksa null")


class PortfolioOut(BaseModel):
    sites: list[PortfolioSite]
    total_units: int
    total_charged: Money = Field(description="finans izni olan sitelerin toplamı")
    total_collected: Money
    collection_rate: str
    total_open_balance: Money


@router.get("/portfolio", summary="Portföy: eriştiği sitelerin özeti")
async def portfolio(access: UserAccessDep, factory: FactoryDep, now: NowDep) -> PortfolioOut:
    if not access.can_see_portfolio:
        raise NotFoundError
    sites = sorted(access.sites, key=lambda s: s.name)
    rows = {
        r.site_id: r
        for r in await svc.portfolio(
            factory,
            site_ids=[s.site_id for s in sites],
            finance_sites=[s.site_id for s in sites if s.can(Permission.FINANCE_READ)],
            request_sites=[s.site_id for s in sites if s.can(Permission.REQUESTS_READ)],
            now=now,
        )
    }
    out = []
    for site in sites:
        row = rows[site.site_id]
        finance = row.finance
        out.append(
            PortfolioSite(
                site_id=site.site_id,
                name=site.name,
                slug=site.slug,
                role=site.role.value,
                units=row.units,
                finance=SiteFinanceOut(
                    charged=finance.charged,
                    collected=finance.collected,
                    collection_rate=f"{finance.collection_rate:.2f}",
                    open_balance=finance.open_balance,
                    debtor_count=finance.debtor_count,
                )
                if finance
                else None,
                requests=SiteRequestsOut(open=row.requests.open, overdue=row.requests.overdue)
                if row.requests
                else None,
            )
        )
    finances = [r.finance for r in rows.values() if r.finance]
    charged = sum((f.charged for f in finances), ZERO)
    collected = sum((f.collected for f in finances), ZERO)
    return PortfolioOut(
        sites=out,
        total_units=sum(r.units for r in rows.values()),
        total_charged=charged,
        total_collected=collected,
        collection_rate=f"{percent(collected, charged):.2f}",
        total_open_balance=sum((f.open_balance for f in finances), ZERO),
    )
