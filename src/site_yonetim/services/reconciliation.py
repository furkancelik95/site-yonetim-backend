"""Gece bakiye mutabakatı — docs/08 §2: özet tablolar defterle tutuyor mu; tutmuyorsa alarm.

Tek doğruluk kaynağı hareket defterleridir; özetler onlardan türetilir:

- `account_balances`  ← `ledger_entries` (borç/alacak toplamı, bakiye)
- `cash_balances`     ← `cash_movements` (giriş/çıkış toplamı, bakiye)
- `site_finance_summary` ← geçerli tahakkuk koşularının borcu (dönemine göre) + onaylı
  tahsilat (tarihinin ayına göre) — göç 0010'daki doldurma tanımıyla aynı

Denetim **bilinçli tüm siteler kapsamında** üç toplu sorgudur (site başına döngü yok, docs/08
§3); gece, trafik yokken çalışır. Onarım yalnız tutmayan sitelerde ve özet satırları kilitlenerek
yapılır: eşzamanlı yazım ya onarımdan önce biter (onarım onu görür) ya da onarımı bekler
(üstüne ekler) — kayıp güncelleme olmaz.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.models import CashAccount
from site_yonetim.services import cash, ledger

Kind = Literal["account", "cash", "summary"]


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: Kind
    site_id: uuid.UUID
    key: str  # hesap kimliği ya da "YYYY-MM"
    field: str
    expected: Decimal  # defterden
    actual: Decimal  # özet tablodaki


_ACCOUNTS = text(
    """
    WITH ledger AS (
        SELECT site_id, account_id, SUM(debit) AS debit, SUM(credit) AS credit
        FROM ledger_entries GROUP BY site_id, account_id
    )
    SELECT COALESCE(l.site_id, b.site_id), COALESCE(l.account_id, b.account_id),
           COALESCE(l.debit, 0), COALESCE(l.credit, 0),
           COALESCE(b.debit_total, 0), COALESCE(b.credit_total, 0), COALESCE(b.balance, 0)
    FROM ledger l FULL JOIN account_balances b ON b.account_id = l.account_id
    WHERE COALESCE(l.debit, 0) <> COALESCE(b.debit_total, 0)
       OR COALESCE(l.credit, 0) <> COALESCE(b.credit_total, 0)
       OR COALESCE(l.debit, 0) - COALESCE(l.credit, 0) <> COALESCE(b.balance, 0)
    """
)
_CASH = text(
    """
    WITH movements AS (
        SELECT site_id, cash_account_id, SUM(inflow) AS inflow, SUM(outflow) AS outflow
        FROM cash_movements GROUP BY site_id, cash_account_id
    )
    SELECT COALESCE(m.site_id, b.site_id), COALESCE(m.cash_account_id, b.cash_account_id),
           COALESCE(m.inflow, 0), COALESCE(m.outflow, 0),
           COALESCE(b.inflow_total, 0), COALESCE(b.outflow_total, 0), COALESCE(b.balance, 0)
    FROM movements m FULL JOIN cash_balances b ON b.cash_account_id = m.cash_account_id
    WHERE COALESCE(m.inflow, 0) <> COALESCE(b.inflow_total, 0)
       OR COALESCE(m.outflow, 0) <> COALESCE(b.outflow_total, 0)
       OR COALESCE(m.inflow, 0) - COALESCE(m.outflow, 0) <> COALESCE(b.balance, 0)
    """
)
# Göç 0010 doldurma tanımı. `:site_id` boşsa bütün siteler.
_EXPECTED_SUMMARY = """
    SELECT site_id, year, month, SUM(charged) AS charged, SUM(collected) AS collected
    FROM (
        SELECT c.site_id, p.year, p.month, c.amount AS charged, 0 AS collected
        FROM charges c
        JOIN charge_runs r ON r.site_id = c.site_id AND r.id = c.charge_run_id
        JOIN periods p ON p.site_id = r.site_id AND p.id = r.period_id
        WHERE r.status = 'posted' AND r.reversal_of_run_id IS NULL
          AND (CAST(:site_id AS uuid) IS NULL OR c.site_id = CAST(:site_id AS uuid))
        UNION ALL
        SELECT site_id, EXTRACT(YEAR FROM date)::int, EXTRACT(MONTH FROM date)::int, 0, amount
        FROM payments
        WHERE status = 'confirmed'
          AND (CAST(:site_id AS uuid) IS NULL OR site_id = CAST(:site_id AS uuid))
    ) AS rows
    GROUP BY site_id, year, month
"""
_SUMMARY = text(
    f"""
    WITH expected AS ({_EXPECTED_SUMMARY})
    SELECT COALESCE(e.site_id, s.site_id), COALESCE(e.year, s.year), COALESCE(e.month, s.month),
           COALESCE(e.charged, 0), COALESCE(e.collected, 0),
           COALESCE(s.charged, 0), COALESCE(s.collected, 0)
    FROM expected e
    FULL JOIN site_finance_summary s
      ON s.site_id = e.site_id AND s.year = e.year AND s.month = e.month
    WHERE COALESCE(e.charged, 0) <> COALESCE(s.charged, 0)
       OR COALESCE(e.collected, 0) <> COALESCE(s.collected, 0)
    """  # noqa: S608  # nosec B608 — sabit SQL parçası, kullanıcı girdisi yok
)


def _compare(
    kind: Kind, site_id: uuid.UUID, key: str, pairs: dict[str, tuple[Decimal, Decimal]]
) -> list[Mismatch]:
    return [
        Mismatch(kind, site_id, key, name, expected, actual)
        for name, (expected, actual) in pairs.items()
        if expected != actual
    ]


async def find_mismatches(factory: async_sessionmaker[AsyncSession]) -> list[Mismatch]:
    found: list[Mismatch] = []
    with all_sites_scope():
        async with factory() as session:
            for (
                site_id,
                account_id,
                debit,
                credit,
                debit_total,
                credit_total,
                balance,
            ) in await session.execute(_ACCOUNTS):
                found += _compare(
                    "account",
                    site_id,
                    str(account_id),
                    {
                        "debit_total": (debit, debit_total),
                        "credit_total": (credit, credit_total),
                        "balance": (debit - credit, balance),
                    },
                )
            for (
                site_id,
                account_id,
                inflow,
                outflow,
                inflow_total,
                outflow_total,
                balance,
            ) in await session.execute(_CASH):
                found += _compare(
                    "cash",
                    site_id,
                    str(account_id),
                    {
                        "inflow_total": (inflow, inflow_total),
                        "outflow_total": (outflow, outflow_total),
                        "balance": (inflow - outflow, balance),
                    },
                )
            for (
                site_id,
                year,
                month,
                charged,
                collected,
                s_charged,
                s_collected,
            ) in await session.execute(_SUMMARY, {"site_id": None}):
                found += _compare(
                    "summary",
                    site_id,
                    f"{year:04d}-{month:02d}",
                    {"charged": (charged, s_charged), "collected": (collected, s_collected)},
                )
    return found


# --- Onarım (yalnız tutmayan sitelerde) ---------------------------------------------

_CASH_REBUILD = text(
    """
    UPDATE cash_balances b SET
        inflow_total = COALESCE(t.inflow, 0),
        outflow_total = COALESCE(t.outflow, 0),
        balance = COALESCE(t.inflow, 0) - COALESCE(t.outflow, 0),
        updated_at = now()
    FROM cash_balances x
    LEFT JOIN (
        SELECT cash_account_id, SUM(inflow) AS inflow, SUM(outflow) AS outflow
        FROM cash_movements WHERE site_id = :site_id GROUP BY cash_account_id
    ) t ON t.cash_account_id = x.cash_account_id
    WHERE b.id = x.id AND b.site_id = :site_id
    """
)
_SUMMARY_LOCK = text(
    "SELECT id FROM site_finance_summary WHERE site_id = :site_id ORDER BY id FOR UPDATE"
)
_SUMMARY_REBUILD = text(
    f"""
    WITH expected AS ({_EXPECTED_SUMMARY}),
    upserted AS (
        INSERT INTO site_finance_summary (id, site_id, year, month, charged, collected)
        SELECT gen_random_uuid(), site_id, year, month, charged, collected FROM expected
        ON CONFLICT (site_id, year, month) DO UPDATE SET
            charged = EXCLUDED.charged, collected = EXCLUDED.collected, updated_at = now()
        RETURNING id
    )
    UPDATE site_finance_summary s SET charged = 0, collected = 0, updated_at = now()
    WHERE s.site_id = CAST(:site_id AS uuid)
      AND NOT EXISTS (
          SELECT 1 FROM expected e WHERE e.year = s.year AND e.month = s.month
      )
      AND (s.charged <> 0 OR s.collected <> 0)
    """  # noqa: S608  # nosec B608 — sabit SQL parçası, kullanıcı girdisi yok
)


async def repair(
    factory: async_sessionmaker[AsyncSession], site_ids: Collection[uuid.UUID]
) -> None:
    """Sitenin üç özetini defterden yeniden üretir; site başına tek transaction."""
    for site_id in sorted(set(site_ids)):
        with site_scope(site_id):
            async with factory() as session, session.begin():
                await ledger.rebuild_balances(session)
                accounts = list(await session.scalars(select(CashAccount.id)))
                if accounts:
                    await cash.lock_accounts(session, accounts)
                    await session.execute(_CASH_REBUILD, {"site_id": site_id})
                await session.execute(_SUMMARY_LOCK, {"site_id": site_id})
                await session.execute(_SUMMARY_REBUILD, {"site_id": site_id})
