"""İstek bağımlılıkları — istek yaşam döngüsü (docs/02-mimari.md §4).

    1. Kimlik doğrulama          → 401            (Dilim 2)
    2. Site çözümleme            → yoksa 404      site_context
    3. Erişim kontrolü           → yoksa 404      (Dilim 2; site_context içinde, çözümlemeden sonra)
    4. Kapsamı aç                → site_scope     site_context
    5. Modül kontrolü            → kapalıysa 404  require_module
    6. İzin kontrolü             → yoksa 403      (Dilim 2)

UYARI: Erişim kontrolü (3) gelene kadar `site_context` herkese açık bir uç noktaya
bağlanmaz; yalnız testlerde ve kimlik doğrulaması olan uçlarda kullanılır.
"""

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.errors import NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Site
from site_yonetim.services.sites import available_modules, find_site_by_slug

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("Veritabanı yapılandırılmamış (DATABASE_URL).")
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class SiteContext:
    """Çözümlenmiş site. Uç nokta çalışırken bu sitenin kapsamı açıktır."""

    site: Site
    modules: frozenset[ModuleKey]
    session: AsyncSession


async def site_context(slug: str, session: SessionDep) -> AsyncIterator[SiteContext]:
    # Biçimsiz slug da "yok" sayılır: 422 yerine 404, varlık sızmasın.
    site = await find_site_by_slug(session, slug) if _SLUG.match(slug) else None
    if site is None:
        raise NotFoundError
    with site_scope(site.id):
        modules = await available_modules(session, site)
        yield SiteContext(site=site, modules=modules, session=session)


SiteContextDep = Annotated[SiteContext, Depends(site_context)]


def require_module(key: ModuleKey) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """Uç noktanın modülü sitede kapalıysa ya da planda yoksa 404 (docs/05 §7).

    Kullanım: `ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]`
    """

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if key not in ctx.modules:
            raise NotFoundError
        return ctx

    return dependency
