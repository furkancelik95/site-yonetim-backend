"""Gecikme tazminatı tahakkuku (KMK m.20; docs/04 §6; Yazılım Dokümanı FIN-03, §8.8; PM K2).

Site kararıyla açılır (finans parametresi `late_fee_enabled`, **varsayılan kapalı**). Açıkken:

- **Ay sonu işi** (`cli run-finance-jobs` içinde, dönem başına bir kez): gecikmiş her hesap için
  o aya ait **tek** `ledger_entries(debit, source = late_fee)`.
- **Ödeme anında:** tahsilattan önce o güne kadar biriken tazminat yazılır, sonra tahsilat mahsup
  sırasına göre dağıtılır (`services/payments.record_payment`).

Hesap ana borç bazındadır (basit — tazminata tazminat yok): bir ana borca o güne kadar işlemiş
**toplam** gecikme hesaplanır, daha önce yazılmış olan (`late_fee_accruals`) düşülür, yalnız fark
yazılır — aynı gün ikinci çalışma bir şey yazmaz (FIN-03 S3). Kısmi ödeme ve plan kapatmaları
açık tutarı düşürür. Ödeme planı süresince (plan bozulmadıkça) taksitlere gecikme işlemez
(`plan_suspends_late_fee`). Oran ve yöntemin hukuki teyidi açık: avukat görüşüne kadar parametre
(docs/12 K2); oran KMK m.20 üst sınırı olan aylık %5'i aşamaz.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.charging.payments import LateFeeRule, accrued_late_fee
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_date_tr
from site_yonetim.models import FinanceParameterVersion, LateFeeAccrual, LateFeeRun, LedgerEntry
from site_yonetim.services import finance_params, ledger, period_lock

# Gecikme işleyen ana borçlar: tahakkuk, dönem dışı ücret, devir ve ödeme planı taksiti.
PRINCIPAL_SOURCES = (
    LedgerSource.CHARGE.value,
    LedgerSource.AD_HOC_CHARGE.value,
    LedgerSource.OPENING.value,
    LedgerSource.PAYMENT_PLAN.value,
)

_PRINCIPALS = text(
    """
    SELECT e.id, e.account_id, e.debit, COALESCE(e.due_date, e.date) AS due, e.date
    FROM ledger_entries e
    WHERE e.site_id = :site_id AND e.debit > 0 AND e.source = ANY(CAST(:sources AS text[]))
      AND (CAST(:account_ids AS uuid[]) IS NULL OR e.account_id = ANY(CAST(:account_ids AS uuid[])))
      AND e.date <= :as_of
      AND COALESCE(e.due_date, e.date) < :late_before
      AND NOT EXISTS (
          SELECT 1 FROM ledger_entries r
          WHERE r.site_id = e.site_id AND r.reversal_of_entry_id = e.id
      )
      AND (NOT :suspend_plans OR NOT EXISTS (
          SELECT 1 FROM payment_plan_installments i
          JOIN payment_plans p ON p.site_id = i.site_id AND p.id = i.plan_id
          WHERE i.site_id = e.site_id AND i.ledger_entry_id = e.id
            AND p.status = 'active' AND p.broken_at IS NULL
      ))
    ORDER BY e.account_id, due, e.created_at, e.id
    """
)
_REDUCTIONS = text(
    """
    SELECT pa.ledger_entry_id AS entry_id, p.date AS day, pa.amount
    FROM payment_allocations pa
    JOIN payments p ON p.site_id = pa.site_id AND p.id = pa.payment_id
    WHERE pa.site_id = :site_id AND pa.ledger_entry_id = ANY(CAST(:ids AS uuid[]))
    UNION ALL
    SELECT c.ledger_entry_id,
           CASE WHEN c.kind = 'restructure' AND p.activated_on IS NOT NULL THEN p.activated_on
                ELSE CAST(COALESCE(p.cancelled_at, c.created_at) AT TIME ZONE 'Europe/Istanbul'
                          AS date)
           END,
           c.amount
    FROM payment_plan_closures c
    JOIN payment_plans p ON p.site_id = c.site_id AND p.id = c.plan_id
    WHERE c.site_id = :site_id AND c.ledger_entry_id = ANY(CAST(:ids AS uuid[]))
    """
)
_POSTED = text(
    """
    SELECT principal_entry_id, SUM(amount) AS total FROM late_fee_accruals
    WHERE site_id = :site_id AND principal_entry_id = ANY(CAST(:ids AS uuid[]))
    GROUP BY principal_entry_id
    """
)


def _site_id() -> uuid.UUID | None:
    scope = current_scope()
    return scope.site_id if scope else None


def rule_of(params: dict[str, Any]) -> LateFeeRule:
    return LateFeeRule(
        monthly_rate_percent=Decimal(str(params["late_fee_monthly_rate_percent"])),
        grace_days=int(params["late_fee_grace_days"]),
        method=str(params["late_fee_method"]),
        cap_percent=int(params["late_fee_cap_percent"]),
        minimum_amount=Decimal(str(params["late_fee_minimum_amount"])),
    )


async def enabled_since(session: AsyncSession, period: YearMonth) -> date | None:
    """Gecikmenin kesintisiz açık olduğu ilk günü (yöntem yürürlüğü — "yürürlükten sonraki
    gecikmeleri etkiler"). O dönemde kapalıysa None."""
    versions = list(
        await session.scalars(
            select(FinanceParameterVersion)
            .where(FinanceParameterVersion.status == finance_params.ACTIVE)
            .order_by(
                FinanceParameterVersion.effective_year,
                FinanceParameterVersion.effective_month,
                FinanceParameterVersion.created_at,
            )
        )
    )
    since: date | None = None
    for version in versions:
        start = YearMonth(version.effective_year, version.effective_month)
        if start > period:
            break
        if (version.values or {}).get("late_fee_enabled", False):
            since = since or start.first_day
        else:
            since = None
    return since


@dataclass(frozen=True, slots=True)
class Accrued:
    account_count: int
    total: Decimal


async def accrue(
    session: AsyncSession,
    *,
    as_of: date,
    entry_date: date,
    description: str,
    account_ids: list[uuid.UUID] | None = None,
) -> Accrued:
    """`as_of` gününe kadar işlemiş gecikmenin henüz yazılmamış kısmını hesap başına tek borç
    hareketi olarak yazar. Gecikme o dönemde kapalıysa hiçbir şey yazmaz."""
    period = YearMonth.of(as_of)
    params = await finance_params.params_at(session, period)
    if not params["late_fee_enabled"]:
        return Accrued(0, ZERO)
    floor = await enabled_since(session, period)
    rule = rule_of(params)
    site_id = _site_id()
    rows = list(
        await session.execute(
            _PRINCIPALS,
            {
                "site_id": site_id,
                "sources": list(PRINCIPAL_SOURCES),
                "account_ids": sorted(account_ids) if account_ids is not None else None,
                "as_of": as_of,
                "late_before": as_of - timedelta(days=rule.grace_days),
                "suspend_plans": bool(params["plan_suspends_late_fee"]),
            },
        )
    )
    if not rows:
        return Accrued(0, ZERO)
    ids = [row.id for row in rows]
    reductions: dict[uuid.UUID, list[tuple[date, Decimal]]] = defaultdict(list)
    for r in await session.execute(_REDUCTIONS, {"site_id": site_id, "ids": ids}):
        reductions[r.entry_id].append((r.day, r.amount))
    posted = {
        r.principal_entry_id: r.total
        for r in await session.execute(_POSTED, {"site_id": site_id, "ids": ids})
    }
    by_account: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal, date]]] = defaultdict(list)
    for row in rows:
        total = accrued_late_fee(
            row.debit, row.due, reductions[row.id], as_of, rule, start_floor=floor
        )
        increment = total - posted.get(row.id, ZERO)
        if increment > ZERO:
            by_account[row.account_id].append((row.id, increment, row.due))
    if not by_account:
        return Accrued(0, ZERO)
    await ledger.lock_accounts(session, list(by_account))
    grand = ZERO
    for account_id, parts in by_account.items():
        amount = sum((p[1] for p in parts), ZERO)
        entry = LedgerEntry(
            id=uuid.uuid7(),
            account_id=account_id,
            date=entry_date,
            # Aynı vadede önce gecikme kapanır (TBK m.100): vadesi en eski ana borcunki.
            due_date=min(p[2] for p in parts),
            debit=amount,
            source=LedgerSource.LATE_FEE.value,
            description=description,
        )
        session.add(entry)
        session.add_all(
            LateFeeAccrual(
                ledger_entry_id=entry.id, principal_entry_id=pid, amount=part, as_of=as_of
            )
            for pid, part, _ in parts
        )
        grand += amount
    await ledger.refresh_balances(session, list(by_account))
    return Accrued(len(by_account), grand)


async def accrue_at_payment(session: AsyncSession, account_id: uuid.UUID, day: date) -> Accrued:
    """Tahsilattan önce o güne kadar biriken tazminat (docs/04 §6 "ödeme anında")."""
    return await accrue(
        session,
        as_of=day,
        entry_date=day,
        description=f"Gecikme tazminatı — {format_date_tr(day)} tahsilatına kadar (KMK m.20)",
        account_ids=[account_id],
    )


@dataclass(frozen=True, slots=True)
class MonthEnd:
    period: YearMonth
    account_count: int
    total: Decimal
    message: str | None


async def run_month_end(session: AsyncSession, today: date, now: Any) -> MonthEnd | None:
    """Önceki ayın ay sonu gecikme işi — dönem başına bir kez. Kapalıysa ya da çalıştıysa None.

    Ay sonu günü kilitli döneme düşüyorsa hareket bugünün tarihiyle açık döneme "önceki döneme
    ait" açıklamasıyla yazılır (FIN-05/FIN-08 ilkesi)."""
    period = YearMonth.of(today).previous()
    params = await finance_params.params_at(session, period)
    if not params["late_fee_enabled"]:
        return None
    done = await session.scalar(
        select(LateFeeRun.id).where(
            LateFeeRun.year == period.year, LateFeeRun.month == period.month
        )
    )
    if done is not None:
        return None
    as_of = period.last_day
    entry_date, suffix, message = as_of, "", None
    try:
        await period_lock.ensure_writable(session, as_of)
    except FinanceRuleError:
        entry_date, suffix = today, " (önceki döneme ait)"
        message = f"{period.name} kilitli; gecikme açık döneme önceki döneme ait olarak yazıldı."
    result = await accrue(
        session,
        as_of=as_of,
        entry_date=entry_date,
        description=f"Gecikme tazminatı — {period.name} (KMK m.20){suffix}",
    )
    session.add(
        LateFeeRun(
            year=period.year,
            month=period.month,
            account_count=result.account_count,
            total=result.total,
            message=message,
            ran_at=now,
        )
    )
    await session.flush()
    return MonthEnd(period, result.account_count, result.total, message)
