"""Pano özet tablosu (`site_finance_summary`, docs/08 §2). Açık site kapsamında.

`INSERT … ON CONFLICT DO UPDATE` ile atomik artım: eşzamanlı iki yazım birbirini ezmez.
Defterden yeniden üretilebilir: göç 0010'daki doldurma sorgusu aynı tanımı kullanır.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import TenantScopeError, current_scope
from site_yonetim.domain.money import ZERO

_ADD = text(
    """
    INSERT INTO site_finance_summary (id, site_id, year, month, charged, collected)
    VALUES (:id, :site_id, :year, :month, :charged, :collected)
    ON CONFLICT (site_id, year, month) DO UPDATE SET
        charged = site_finance_summary.charged + EXCLUDED.charged,
        collected = site_finance_summary.collected + EXCLUDED.collected,
        updated_at = now()
    """
)


def _site_id() -> uuid.UUID:
    scope = current_scope()
    if scope is None or scope.site_id is None:
        raise TenantScopeError("Özet tablo yalnız tek bir site kapsamında güncellenir.")
    return scope.site_id


async def add(
    session: AsyncSession,
    *,
    year: int,
    month: int,
    charged: Decimal = ZERO,
    collected: Decimal = ZERO,
) -> None:
    await session.execute(
        _ADD,
        {
            "id": uuid.uuid7(),
            "site_id": _site_id(),
            "year": year,
            "month": month,
            "charged": charged,
            "collected": collected,
        },
    )


async def add_collected(session: AsyncSession, day: date, amount: Decimal) -> None:
    await add(session, year=day.year, month=day.month, collected=amount)
