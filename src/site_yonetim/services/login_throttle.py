"""IP bazlı giriş hız sınırı — docs/09 §4 (hesap kilidinin yanında ikinci katman).

Hesap kilidi tek hesaba yönelik denemeyi durdurur; bu sınır **bir adresten çok hesaba**
denemeyi (parola püskürtme) durdurur.

`guard` adresin sayaç satırını giriş boyunca kilitler (ayrı, kısa bir transaction): aynı
adresten gelen girişler sıraya girer, paralel denemeler sınırı aşamaz. Yalnız **hatalı** deneme
sayılır; başarılı giriş sayaca dokunmaz (geçerli bir hesapla araya başarılı giriş sokarak
sayaç sıfırlanamaz). Giriş transaction'ı geri alınsa da hatalı deneme sayılmış kalır.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import case, delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.models import LoginThrottle
from site_yonetim.services.auth import (
    AccountLockedError,
    InvalidCredentialsError,
    TooManyAttemptsError,
)

UNKNOWN_IP = "unknown"
# Sayılan hatalar: yanlış parola / bilinmeyen e-posta / kilitli hesaba deneme.
_FAILURES = (InvalidCredentialsError, AccountLockedError)


@asynccontextmanager
async def guard(
    factory: async_sessionmaker[AsyncSession],
    *,
    ip: str | None,
    now: datetime,
    limit: int,
    window: timedelta,
) -> AsyncIterator[None]:
    """Sınır doluysa `TooManyAttemptsError` (parola hiç denenmez); blok hatalı girişle
    biterse sayaç bir artar."""
    key = ip or UNKNOWN_IP
    expired = LoginThrottle.window_start <= now - window
    # ON CONFLICT DO UPDATE satırı transaction sonuna kadar kilitler; pencere dolduysa sıfırlar.
    lock = (
        insert(LoginThrottle)
        .values(ip=key, window_start=now, failures=0)
        .on_conflict_do_update(
            index_elements=[LoginThrottle.ip],
            set_={
                "failures": case((expired, 0), else_=LoginThrottle.failures),
                "window_start": case((expired, now), else_=LoginThrottle.window_start),
            },
        )
        .returning(LoginThrottle.failures)
    )
    async with factory() as session:
        failures = (await session.execute(lock)).scalar_one()
        if failures >= limit:
            await session.commit()
            raise TooManyAttemptsError(int(window.total_seconds()))
        try:
            yield
        except _FAILURES:
            await session.execute(
                update(LoginThrottle)
                .where(LoginThrottle.ip == key)
                .values(failures=LoginThrottle.failures + 1)
            )
            await session.commit()
            raise
        await session.commit()


async def purge(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, window: timedelta
) -> int:
    """Penceresi dolmuş sayaçları siler (gece işi)."""
    async with factory() as session, session.begin():
        result = await session.execute(
            delete(LoginThrottle).where(LoginThrottle.window_start <= now - window)
        )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
