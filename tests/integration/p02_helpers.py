"""Yapı, kişiler ve kayıt paketi (servis istekleri 19–26) testlerinin ortak yardımcıları.

`world` kurgusu (finance_world): Aksu Konakları — A bloğu, A-1 bölümü, malik Ayşe YILMAZ (%100),
yönetici Kerem YILDIRIM; ikinci site Yıldız Sitesi (izolasyon için).
"""

import uuid
from typing import Any

import httpx2
from sqlalchemy import update

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Plan, Site, SiteModule
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

OTHER = "yildiz-sitesi"


async def staff(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email, full_name="Test DENEME")
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def resident(w: World, email: str, person_id: str) -> dict[str, str]:
    user = await create_user(w.factory, email, full_name="Ayşe YILMAZ", kind="resident")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(person_id)
    )
    return await login_headers(w.api, email)


async def call(
    w: World,
    method: str,
    path: str,
    body: object = None,
    *,
    headers: dict[str, str] | None = None,
    slug: str = "aksu-konaklari",
    **kwargs: Any,
) -> httpx2.Response:
    return await w.api.request(
        method, w.url(path, slug), json=body, headers=headers or w.headers, **kwargs
    )


async def new_unit(w: World, number: str, *, owner: str | None = "Mehmet Kaya") -> str:
    unit = (await w.post("/units", {"block_id": w.lookups["block"], "number": number})).json()
    unit_id = str(unit["data"]["id"])
    if owner:
        first, last = owner.split()
        response = await w.post(
            f"/units/{unit_id}/parties",
            {
                "role": "owner",
                "start_date": "2020-01-01",
                "person": {"first_name": first, "last_name": last},
            },
        )
        assert response.status_code == 201, response.text
    return unit_id


async def set_lifecycle(w: World, status: str) -> None:
    async with w.factory() as session, session.begin():
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.lifecycle_status = status


def scope(w: World) -> Any:
    return site_scope(w.site_id)


async def enable_modules(w: World, *keys: str) -> None:
    """Siteye modülleri içeren bir plan bağlar ve modülleri açar."""
    async with w.factory() as session, session.begin():
        plan = Plan(name=f"Plan {uuid.uuid4().hex[:6]}", allowed_modules=list(keys))
        session.add(plan)
        await session.flush()
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.plan_id = plan.id
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            await session.execute(
                update(SiteModule).where(SiteModule.module_key.in_(keys)).values(enabled=True)
            )
