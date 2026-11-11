"""Plan kapsamı — docs/01 "Planlar": yalnız şirket planı portföyü açar."""

from site_yonetim.domain.modules import ModuleKey, PlanScope, plan_scope
from site_yonetim.seed.demo_data import ORGANIZATION_PLAN, PLANS


def test_demo_planlarinda_yalniz_sirket_plani_organization() -> None:
    scopes = {p.name: plan_scope(m.value for m in p.modules) for p in PLANS}

    assert scopes == {
        "Başlangıç": PlanScope.SITE,
        "Standart": PlanScope.SITE,
        "Pro": PlanScope.SITE,
        ORGANIZATION_PLAN: PlanScope.ORGANIZATION,
    }


def test_portfoy_olmayan_plan_site_plani() -> None:
    assert plan_scope([]) is PlanScope.SITE
    assert plan_scope([ModuleKey.FINANCE.value]) is PlanScope.SITE
    assert plan_scope(["finance", "portfolio"]) is PlanScope.ORGANIZATION
