"""Herkese açık uçlar için istek sınırı — sabit pencere, anahtar başına sayaç (PostgreSQL).

Anahtar uç ve IP'yi birlikte taşır (`registration-post:203.0.113.9`). Sayaç tek ifadede atomik
artar (`INSERT … ON CONFLICT DO UPDATE … RETURNING`); pencere dolduysa sıfırdan başlar. Birden çok
API kopyası aynı sayacı görür. Eski satırları gece işi siler (`purge-login-throttle`).
"""

from datetime import datetime, timedelta

from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.models import RateLimit


async def hit(
    factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    now: datetime,
    limit: int,
    window: timedelta,
) -> bool:
    """İsteği sayar; pencerede `limit` aşıldıysa False (istek reddedilir)."""
    expired = RateLimit.window_start <= now - window
    statement = (
        insert(RateLimit)
        .values(key=key[:200], window_start=now, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimit.key],
            set_={
                "count": case((expired, 1), else_=RateLimit.count + 1),
                "window_start": case((expired, now), else_=RateLimit.window_start),
            },
        )
        .returning(RateLimit.count)
    )
    async with factory() as session, session.begin():
        count = (await session.execute(statement)).scalar_one()
    return count <= limit


async def purge(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, older_than: timedelta
) -> int:
    async with factory() as session, session.begin():
        result = await session.execute(
            delete(RateLimit).where(RateLimit.window_start <= now - older_than)
        )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
