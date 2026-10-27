"""Muhasebe aktarımı — Mod P fiş dosyası (FIN-14, §8.4; servis isteği 32). Açık site kapsamında.

Fiş kaynakları (dönem içi): tahakkuk koşusu (koşu başına bir fiş; ters kayıt koşusu ters fiş),
tahsilat (yöntem bazında), ödenmiş gider (ödeme tarihi; ödenmiş giderin düzeltmesi ters fiş),
dönem dışı ücret (iptali ters fiş) ve iade. Her fiş tek borç + tek alacak satırıdır.

- Her fiş bir kez aktarılır ve kilitlenir (`accounting_export_items.voucher_key` tekil); aynı
  dönem ikinci kez aktarılırsa yalnız yeni fişler gelir.
- Eşlemesi eksik işlem tipi varsa aktarım başlatılmaz (409 `mapping_missing`).
- Eşleme versiyonludur; değişiklik yalnız sonraki dosyaları etkiler (fiş kendi kodunu taşır).
- Dosya biçimi genel CSV (K7 — Kivi biçimi görüşmeden sonra `AccountingExportAdapter` ile).
"""

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.accounting import (
    ADAPTERS,
    DEFAULT_MAPPINGS,
    LABELS,
    MAPPING_KEYS,
    MappingDef,
    Voucher,
    check_code,
)
from site_yonetim.domain.charging.payments import PaymentStatus
from site_yonetim.domain.charging.periods import YearMonth, reversal_description
from site_yonetim.domain.finance import ChargeRunStatus, LedgerSource
from site_yonetim.domain.fiscal import period_key
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.models import (
    AccountingExport,
    AccountingExportItem,
    AccountingMappingVersion,
    Charge,
    ChargeRun,
    Expense,
    LedgerAccount,
    LedgerEntry,
    Payment,
    Period,
    Refund,
)
from site_yonetim.services.approvals import Person

DESCRIPTION_MAX = 200


# --- Eşleme -------------------------------------------------------------------------------


async def mappings(session: AsyncSession) -> list[MappingDef]:
    """Geçerli eşleme (son sürüm; sürümde olmayan anahtar varsayılan öneriden)."""
    latest = await session.scalar(
        select(AccountingMappingVersion)
        .order_by(AccountingMappingVersion.created_at.desc())
        .limit(1)
    )
    stored = {i["key"]: i for i in (latest.items if latest else [])}
    out: list[MappingDef] = []
    for default in DEFAULT_MAPPINGS:
        item = stored.get(default.key)
        out.append(
            replace(
                default,
                debit_account=item["debit_account"],
                credit_account=item["credit_account"],
            )
            if item
            else default
        )
    return out


async def save_mappings(
    session: AsyncSession, items: list[tuple[str, str, str]], *, actor: Person
) -> list[MappingDef]:
    """`(key, borç, alacak)` listesi; verilmeyen anahtar eski değerini korur. Yeni sürüm yazar."""
    current = {m.key: m for m in await mappings(session)}
    errors: dict[str, str] = {}
    for key, debit, credit in items:
        if key not in MAPPING_KEYS:
            errors[key] = "Bu işlem tipi tanınmıyor."
            continue
        for field, value in (("debit_account", debit), ("credit_account", credit)):
            if not check_code(value):
                errors[f"{key}.{field}"] = "Hesap kodu rakam ve nokta (ör. 120.01)."
        current[key] = replace(current[key], debit_account=debit, credit_account=credit)
    if errors:
        raise FormFieldError(errors)
    session.add(
        AccountingMappingVersion(
            items=[
                {"key": m.key, "debit_account": m.debit_account, "credit_account": m.credit_account}
                for m in current.values()
            ],
            created_by=actor.name,
        )
    )
    await session.flush()
    return list(current.values())


# --- Fişler -------------------------------------------------------------------------------


def _clip(text: str) -> str:
    return " ".join(text.split())[:DESCRIPTION_MAX]


async def _all_vouchers(
    session: AsyncSession, period: YearMonth, by_key: dict[str, MappingDef]
) -> list[Voucher]:
    first, last = period.first_day, period.last_day
    out: list[Voucher] = []

    def add(
        key: str,
        source: str,
        source_id: uuid.UUID,
        day: date,
        text: str,
        amount: Decimal,
        *,
        reverse: bool = False,
    ) -> None:
        mapping = by_key[source]
        debit, credit = mapping.debit_account, mapping.credit_account
        if reverse:
            debit, credit = credit, debit
        out.append(
            Voucher(
                f"{key}:{source_id}",
                source,
                str(source_id),
                day,
                _clip(text),
                debit,
                credit,
                amount,
            )
        )

    # Tahakkuk koşuları: orijinal koşu ve ters kayıt koşusu (ters fiş).
    totals = (
        select(Charge.charge_run_id, func.sum(Charge.amount).label("total"))
        .group_by(Charge.charge_run_id)
        .subquery()
    )
    runs = await session.execute(
        select(ChargeRun, Period.year, Period.month, totals.c.total)
        .join(Period, and_(Period.id == ChargeRun.period_id, Period.site_id == ChargeRun.site_id))
        .outerjoin(totals, totals.c.charge_run_id == ChargeRun.id)
        .where(
            ChargeRun.charge_date >= first,
            ChargeRun.charge_date <= last,
            ChargeRun.status.in_([ChargeRunStatus.POSTED.value, ChargeRunStatus.REVERSED.value]),
        )
    )
    original_totals: dict[uuid.UUID, Decimal] = {}
    reversals: list[tuple[ChargeRun, YearMonth]] = []
    for run, year, month, total in runs:
        ym = YearMonth(year, month)
        if run.reversal_of_run_id is None:
            if total and total > ZERO:
                add(
                    "charge", "charge", run.id, run.charge_date, f"{ym.name} aidat tahakkuku", total
                )
        else:
            reversals.append((run, ym))
    for run, ym in reversals:
        original = run.reversal_of_run_id
        if original not in original_totals:
            original_totals[original] = (  # type: ignore[index]
                await session.scalar(
                    select(func.coalesce(func.sum(Charge.amount), 0)).where(
                        Charge.charge_run_id == original
                    )
                )
                or ZERO
            )
        amount = original_totals[original]  # type: ignore[index]
        if amount > ZERO:
            add(
                "charge",
                "charge",
                run.id,
                run.charge_date,
                reversal_description(ym),
                amount,
                reverse=True,
            )

    # Tahsilatlar (yöntem bazında).
    payments = await session.execute(
        select(Payment, LedgerAccount.reference_code)
        .join(LedgerAccount, LedgerAccount.id == Payment.ledger_account_id)
        .where(
            Payment.date >= first,
            Payment.date <= last,
            Payment.status == PaymentStatus.CONFIRMED.value,
        )
    )
    for payment, code in payments:
        add(
            "payment",
            f"payment_{payment.method}",
            payment.id,
            payment.date,
            f"Tahsilat {code}" + (f" — {payment.reference}" if payment.reference else ""),
            payment.amount,
        )

    # Ödenmiş giderler; ödenmiş giderin düzeltmesi ters fiş (para kasaya döndü).
    expenses = await session.scalars(
        select(Expense).where(
            Expense.reversal_of_id.is_(None),
            Expense.paid_on >= first,
            Expense.paid_on <= last,
        )
    )
    for expense in expenses:
        add(
            "expense",
            "expense",
            expense.id,
            expense.paid_on or expense.date,
            expense.description,
            expense.amount,
        )
    original_expense = Expense.__table__.alias("original")
    corrections = await session.scalars(
        select(Expense)
        .join(original_expense, original_expense.c.id == Expense.reversal_of_id)
        .where(
            Expense.date >= first,
            Expense.date <= last,
            original_expense.c.paid_on.is_not(None),
            original_expense.c.site_id == Expense.site_id,
        )
    )
    for correction in corrections:
        add(
            "expense",
            "expense",
            correction.id,
            correction.date,
            correction.description,
            -correction.amount,
            reverse=True,
        )

    # Dönem dışı ücret (borç) ve iptali (alacak → ters fiş).
    entries = await session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.source == LedgerSource.AD_HOC_CHARGE.value,
            LedgerEntry.date >= first,
            LedgerEntry.date <= last,
        )
    )
    for entry in entries:
        debit = entry.debit > ZERO
        add(
            "ad_hoc_charge",
            "ad_hoc_charge",
            entry.id,
            entry.date,
            entry.description,
            entry.debit if debit else entry.credit,
            reverse=not debit,
        )

    # İadeler.
    refunds = await session.execute(
        select(Refund, LedgerAccount.reference_code)
        .join(LedgerAccount, LedgerAccount.id == Refund.ledger_account_id)
        .where(Refund.date >= first, Refund.date <= last)
    )
    for refund, code in refunds:
        add(
            "refund",
            "refund",
            refund.id,
            refund.date,
            f"İade {code} — {refund.reason}",
            refund.amount,
        )
    return sorted(out, key=lambda v: (v.date, v.key))


@dataclass(frozen=True, slots=True)
class ExportPreview:
    period: YearMonth
    vouchers: list[Voucher]
    already_exported: int
    total: Decimal
    missing_mappings: list[str]


async def preview(session: AsyncSession, period: YearMonth) -> ExportPreview:
    by_key = {m.key: m for m in await mappings(session)}
    every = await _all_vouchers(session, period, by_key)
    keys = [v.key for v in every]
    exported = (
        set(
            await session.scalars(
                select(AccountingExportItem.voucher_key).where(
                    AccountingExportItem.voucher_key.in_(keys)
                )
            )
        )
        if keys
        else set()
    )
    pending = [v for v in every if v.key not in exported]
    missing = sorted(
        {LABELS[v.source] for v in pending if not v.debit_account or not v.credit_account},
        key=list(LABELS.values()).index,
    )
    total = sum((v.amount for v in pending), ZERO)
    return ExportPreview(period, pending, len(every) - len(pending), total, missing)


class ExportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def export(
    session: AsyncSession, period: YearMonth, *, slug: str, actor: Person, now: datetime
) -> AccountingExport:
    """Aktarılmamış fişleri dosyaya yazar ve kilitler (tek transaction)."""
    pv = await preview(session, period)
    if pv.missing_mappings:
        raise ExportError(
            "mapping_missing",
            f"Hesap kodu eşlemesi eksik: {', '.join(pv.missing_mappings)}. Aktarım başlatılamaz.",
        )
    if not pv.vouchers:
        raise ExportError("nothing_to_export", "Bu dönemde aktarılmamış fiş yok.")
    sequence = (
        await session.scalar(
            select(func.count()).where(
                AccountingExport.year == period.year, AccountingExport.month == period.month
            )
        )
        or 0
    ) + 1
    adapter = ADAPTERS["csv"]
    batch = AccountingExport(
        id=uuid.uuid7(),
        year=period.year,
        month=period.month,
        voucher_count=len(pv.vouchers),
        total=pv.total,
        file_name=f"muhasebe-fisleri-{slug}-{period_key(period)}-{sequence}.{adapter.extension}",
        content=adapter.render(pv.vouchers),
        created_by_id=actor.user_id,
        created_by=actor.name,
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        AccountingExportItem(
            export_id=batch.id,
            voucher_key=v.key,
            source=v.source,
            source_id=v.source_id,
            date=v.date,
            description=v.description,
            debit_account=v.debit_account,
            credit_account=v.credit_account,
            amount=v.amount,
        )
        for v in pv.vouchers
    )
    await session.flush()
    return batch


async def items_of(session: AsyncSession, export_id: uuid.UUID) -> list[Voucher]:
    rows = await session.scalars(
        select(AccountingExportItem)
        .where(AccountingExportItem.export_id == export_id)
        .order_by(AccountingExportItem.date, AccountingExportItem.voucher_key)
    )
    return [
        Voucher(
            r.voucher_key,
            r.source,
            r.source_id,
            r.date,
            r.description,
            r.debit_account,
            r.credit_account,
            r.amount,
        )
        for r in rows
    ]


async def pending_count(session: AsyncSession, period: YearMonth) -> int:
    return len((await preview(session, period)).vouchers)
