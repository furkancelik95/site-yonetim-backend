"""Site çözümleme ve modül durumu servisleri."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.modules import ModuleKey, check_toggle, initial_state, plan_allows
from site_yonetim.models import Plan, Site, SiteModule


async def find_site_by_slug(session: AsyncSession, slug: str) -> Site | None:
    """`sites` global tablodur; kapsam açılmadan okunabilir."""
    site: Site | None = await session.scalar(select(Site).where(Site.slug == slug))
    return site


async def plan_modules(session: AsyncSession, site: Site) -> tuple[str, ...]:
    if site.plan_id is None:
        return ()
    allowed = await session.scalar(select(Plan.allowed_modules).where(Plan.id == site.plan_id))
    return tuple(allowed or ())


def _require_site_scope(site: Site) -> None:
    scope = current_scope()
    if scope is None or scope.site_id != site.id:
        raise RuntimeError("Modül işlemleri yalnız ilgili sitenin kapsamında yapılır.")


async def provision_site_modules(session: AsyncSession, site: Site) -> None:
    """Yeni site kurulumu: her modül için bir satır; çekirdek + planın izin verdiği
    announcements/requests/documents açık, diğerleri kapalı (docs/10 §3.2)."""
    _require_site_scope(site)
    for key, enabled in initial_state(await plan_modules(session, site)).items():
        session.add(SiteModule(module_key=key.value, enabled=enabled))
    await session.flush()


async def available_modules(session: AsyncSession, site: Site) -> frozenset[ModuleKey]:
    """Sitede kullanılabilen modüller: açık **ve** planda (çekirdek her zaman)."""
    _require_site_scope(site)
    rows = await session.scalars(select(SiteModule.module_key).where(SiteModule.enabled))
    enabled = {ModuleKey(value) for value in rows}
    allowed = plan_allows(await plan_modules(session, site))
    return frozenset((enabled & allowed) | {ModuleKey.FINANCE})


async def set_module_enabled(
    session: AsyncSession, site: Site, key: ModuleKey, *, enable: bool
) -> SiteModule:
    """Modülü aç/kapa. Kapatmak veriyi silmez. Kural ihlalinde `ModuleRuleError`."""
    _require_site_scope(site)
    check_toggle(key, enable, await plan_modules(session, site))
    row = await session.scalar(select(SiteModule).where(SiteModule.module_key == key.value))
    if row is None:
        row = SiteModule(module_key=key.value, enabled=enable)
        session.add(row)
    else:
        row.enabled = enable
    await session.flush()
    return row
