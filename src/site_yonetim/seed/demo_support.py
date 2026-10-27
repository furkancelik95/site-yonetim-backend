"""Demo verisi — platform destek (PYN-04, PYN-06; servis istekleri 82–84).

Plan destek seviyeleri, bilgi bankası makaleleri, Aksu'da yanıtlanmış bir destek bileti ve
tesis yöneticisinin onayını bekleyen bir destek oturumu talebi (ekranlar boş görünmesin).
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.support import (
    ArticleAudience,
    SupportTier,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from site_yonetim.models import KnowledgeArticle, Plan, Site, SupportSession
from site_yonetim.services import support as svc

PLAN_TIERS = {
    "Standart": SupportTier.CRITICAL_24_7,
    "Pro": SupportTier.CRITICAL_24_7,
    "Yönetim Şirketi": SupportTier.FULL_24_7,
}

ARTICLES: tuple[tuple[str, ArticleAudience, tuple[str, ...], str], ...] = (
    (
        "Excel ile bölüm aktarımı",
        ArticleAudience.STAFF,
        ("iceri-aktar", "yapi"),
        "Şablonu indirin, her satıra bir bölüm yazın. Önizlemede hatalı satırlar kırmızı "
        "görünür; düzeltip yeniden yükleyin. Aktarım 24 saat içinde geri alınabilir.",
    ),
    (
        "Banka ekstresi eşleştirme",
        ArticleAudience.STAFF,
        ("banka", "tahsilat"),
        "Ekstreyi yükleyin; sistem açıklamadaki daire kodu ve tutara göre eşleşme önerir. "
        "Emin olmadığınız satırları elle eşleştirin, sonra toplu kaydedin.",
    ),
    (
        "Destek oturumu nedir?",
        ArticleAudience.STAFF,
        ("destek",),
        "Platform destek ekibi verinizi yalnız sizin onayınızla, en çok 24 saat ve salt okunur "
        "görebilir. Oturum açıkken her ekranda bant görünür; dilediğiniz an bitirebilirsiniz.",
    ),
    (
        "Aidatımı nasıl öderim?",
        ArticleAudience.RESIDENT,
        ("borcum",),
        "Borcum ekranında güncel borcunuzu ve ekstrenizi görürsünüz. Ödeme yönetimin "
        "bildirdiği banka hesabına daire kodunuzla yapılır; kayıt yönetim tarafından işlenir.",
    ),
)


async def seed_support(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    manager_id: uuid.UUID,
    platform_id: uuid.UUID,
    now: datetime,
) -> None:
    async with factory() as session, session.begin():
        for name, tier in PLAN_TIERS.items():
            await session.execute(
                update(Plan).where(Plan.name == name).values(support_tier=tier.value)
            )
        session.add_all(
            KnowledgeArticle(
                title=title,
                body=body,
                audience=audience.value,
                screens=list(screens),
                updated_by_id=platform_id,
                updated_by_name="Deniz Aksoy",
            )
            for title, audience, screens, body in ARTICLES
        )
    with site_scope(site_id):
        async with factory() as session, session.begin():
            site = await session.get(Site, site_id)
            if site is None:  # pragma: no cover - site az önce açıldı
                raise RuntimeError("Demo sitesi bulunamadı.")
            await _ticket(session, site, manager_id, platform_id, now)
            session.add(
                SupportSession(
                    reason="DST-0001 banka ekstresi eşleşmesi incelemesi",
                    hours=4,
                    requested_by_id=platform_id,
                    requested_by_name="Deniz Aksoy",
                )
            )


async def _ticket(
    session: AsyncSession, site: Site, manager_id: uuid.UUID, platform_id: uuid.UUID, now: datetime
) -> None:
    opened = now - timedelta(hours=3)
    ticket = await svc.open_ticket(
        session,
        site,
        user_id=manager_id,
        user_name="Kerem Yıldırım",
        title="Banka ekstresinde eşleşmeyen satırlar",
        body="Ekim ekstresinde 3 tahsilat daire koduyla eşleşmedi; açıklamalar doğru görünüyor.",
        category=TicketCategory.FINANCE,
        priority=TicketPriority.HIGH,
        path="/s/aksu-konaklari/banka",
        app_version="web",
        user_agent="Demo",
        correlation_id="demo",
        now=opened,
    )
    ticket.created_at = opened
    await svc.support_reply(
        session,
        ticket,
        user_id=platform_id,
        user_name="Deniz Aksoy",
        body="Açıklamalarda daire kodu boşlukla yazılmış. Ekstreyi yeniden yükler misiniz?",
        status=TicketStatus.WAITING_CUSTOMER,
        now=opened + timedelta(hours=1),
    )
