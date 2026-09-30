"""Cari defter ve özet bakiye (docs/03 §5, docs/04 §13, docs/08 §2). Açık site kapsamında.

Kural: hareket (`ledger_entries`) tek doğruluk kaynağıdır; `account_balances` aynı
transaction'da ondan yeniden hesaplanır.

Eşzamanlılık: hesaba yazan her işlem önce `lock_accounts` ile özet satırlarını **hesap kimliği
sırasıyla** kilitler (kilitlenme olmaz). Böylece aynı hesaba yazan ikinci işlem birincinin
commit'ini bekler ve yeniden hesaplarken onun hareketlerini görür — bakiye kaybolmaz, aynı borç
iki kez mahsup edilmez.

Sorgular satır sayısından bağımsız sayıda çalışır; hesap listeleri tek dizi parametresiyle
gider (10.000 hesaplık koşuda parametre sınırına takılmaz).
"""

import uuid
from collections.abc import Collection

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import TenantScopeError, current_scope
from site_yonetim.models import LedgerAccount


def _site_id() -> uuid.UUID:
    scope = current_scope()
    if scope is None or scope.site_id is None:
        raise TenantScopeError("Cari defter işlemi yalnız tek bir site kapsamında çalışır.")
    return scope.site_id


_ENSURE_ROWS = text(
    """
    INSERT INTO account_balances (id, site_id, account_id, debit_total, credit_total, balance)
    SELECT row_id, :site_id, account_id, 0, 0, 0
    FROM unnest(CAST(:row_ids AS uuid[]), CAST(:account_ids AS uuid[])) AS t(row_id, account_id)
    ORDER BY account_id
    ON CONFLICT (account_id) DO NOTHING
    """
)

_LOCK_ROWS = text(
    """
    SELECT account_id FROM account_balances
    WHERE site_id = :site_id AND account_id = ANY(CAST(:account_ids AS uuid[]))
    ORDER BY account_id
    FOR UPDATE
    """
)

# docs/04 §13: borçlar vadeye göre sıralanır, hesabın toplam alacağı bu sırayla düşülür;
# tamamen kapanamayan ilk borcun vadesi "en eski açık vade"dir.
_REFRESH = text(
    """
    WITH accounts AS (
        SELECT unnest(CAST(:account_ids AS uuid[])) AS account_id
    ), totals AS (
        SELECT account_id, SUM(debit) AS debit_total, SUM(credit) AS credit_total
        FROM ledger_entries
        WHERE site_id = :site_id AND account_id = ANY(CAST(:account_ids AS uuid[]))
        GROUP BY account_id
    ), debts AS (
        SELECT account_id, COALESCE(due_date, date) AS due,
               SUM(debit) OVER (
                   PARTITION BY account_id ORDER BY COALESCE(due_date, date), created_at, id
               ) AS cumulative
        FROM ledger_entries
        WHERE site_id = :site_id AND debit > 0
          AND account_id = ANY(CAST(:account_ids AS uuid[]))
    ), oldest AS (
        SELECT d.account_id, MIN(d.due) AS due
        FROM debts d JOIN totals t ON t.account_id = d.account_id
        WHERE d.cumulative > t.credit_total + 0.005
        GROUP BY d.account_id
    )
    UPDATE account_balances AS b SET
        debit_total = COALESCE(t.debit_total, 0),
        credit_total = COALESCE(t.credit_total, 0),
        balance = COALESCE(t.debit_total, 0) - COALESCE(t.credit_total, 0),
        oldest_open_due_date = o.due,
        updated_at = now()
    FROM accounts a
    LEFT JOIN totals t ON t.account_id = a.account_id
    LEFT JOIN oldest o ON o.account_id = a.account_id
    WHERE b.site_id = :site_id AND b.account_id = a.account_id
    """
)


async def lock_accounts(session: AsyncSession, account_ids: Collection[uuid.UUID]) -> None:
    """Özet satırı yoksa açar ve hesapları kilitler. Hesaba yazmadan **önce** çağrılır."""
    ids = sorted(set(account_ids))
    if not ids:
        return
    site_id = _site_id()
    await session.execute(
        _ENSURE_ROWS,
        {"site_id": site_id, "row_ids": [uuid.uuid7() for _ in ids], "account_ids": ids},
    )
    await session.execute(_LOCK_ROWS, {"site_id": site_id, "account_ids": ids})


async def refresh_balances(session: AsyncSession, account_ids: Collection[uuid.UUID]) -> None:
    """Hesapların özetini defterden yeniden hesaplar (hareketler flush edilmiş olmalı)."""
    ids = sorted(set(account_ids))
    if not ids:
        return
    await session.flush()
    await session.execute(_REFRESH, {"site_id": _site_id(), "account_ids": ids})


async def rebuild_balances(session: AsyncSession) -> int:
    """Sitenin tüm özet bakiyelerini defterden yeniden üretir (mutabakat/onarım)."""
    ids = list(await session.scalars(select(LedgerAccount.id)))
    await lock_accounts(session, ids)
    await refresh_balances(session, ids)
    return len(ids)
