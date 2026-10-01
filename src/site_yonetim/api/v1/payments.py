"""Cari hesaplar, tahsilat, ekstre, borçlular, makbuz verisi — docs/06 §2.6, docs/04 §5, §13.

- Tahsilat para yazar: `Idempotency-Key` kabul eder; tekrar gönderimde ilk yanıt döner.
- Ekstre ve tahsilat ayrıntısını `finance.read` izni olan personel **ya da hesabın sahibi
  sakin** görür; diğerleri 403 (docs/05 §6 — güvenlik görevlisi sakin bakiyesini göremez).
- Kişi adları `people.read` izni olmayana `null` döner (Denetçi kişisel veri görmez, docs/05).
- Makbuz numaralandırması açık karar (docs/12 K15): `receipt_number` şimdilik hep `null`.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.charges import LineOut
from site_yonetim.api.v1.finance_common import (
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.charging.payments import PaymentMethod, PaymentStatus
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import LedgerAccount, Payment
from site_yonetim.services import accounts as svc
from site_yonetim.services import payments as payment_svc

router = APIRouter(prefix="/sites/{slug}", tags=["tahsilat ve cari"])
Paging = Annotated[PageParams, Depends()]
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]


def _sees_names(ctx: SiteContext) -> bool:
    return ctx.access.can(Permission.PEOPLE_READ)


def _ensure_can_see(ctx: SiteContext, account: LedgerAccount) -> None:
    """Personel `finance.read` ile, sakin yalnız kendi hesabını görür (docs/05 §6)."""
    own = ctx.access.person_id is not None and ctx.access.person_id == account.person_id
    if not (own or ctx.access.can(Permission.FINANCE_READ)):
        raise ForbiddenError


# --- Hesaplar ---------------------------------------------------------------------


class AccountOut(BaseModel):
    id: uuid.UUID
    reference_code: str = Field(description="havale açıklamasına yazılır: `A12-M`")
    kind: AccountKind = Field(description="owner: malik hesabı · occupant: oturan hesabı")
    unit_id: uuid.UUID
    unit_name: str
    person_id: uuid.UUID
    person_name: str | None = Field(description="people.read izni ya da kendi hesabı; yoksa null")
    balance: Money = Field(description="pozitif = borçlu, negatif = avans")
    oldest_open_due_date: dt.date | None
    is_closed: bool

    @classmethod
    def of(cls, row: svc.AccountRow, *, names: bool) -> AccountOut:
        account = row.account
        return cls(
            id=account.id,
            reference_code=account.reference_code,
            kind=AccountKind(account.kind),
            unit_id=account.unit_id,
            unit_name=row.unit_name,
            person_id=account.person_id,
            person_name=row.person_name if names else None,
            balance=row.balance,
            oldest_open_due_date=row.oldest_open_due_date,
            is_closed=account.is_closed,
        )


async def _page_rows(
    ctx: SiteContext, query: Select[*tuple[Any, ...]], paging: PageParams
) -> tuple[list[svc.AccountRow], int]:
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size))
    return [svc.to_row(tuple(r)) for r in rows], total


@router.get("/accounts", summary="Cari hesaplar ve bakiyeleri (tahsilat girişi için arama)")
async def list_accounts(
    ctx: FinanceRead,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=60, description="referans, bölüm ya da kişi")] = None,
    unit_id: uuid.UUID | None = None,
) -> Page[AccountOut]:
    query = svc.search(svc.accounts_query(), q)
    if unit_id is not None:
        query = query.where(LedgerAccount.unit_id == unit_id)
    rows, total = await _page_rows(ctx, query.order_by(LedgerAccount.reference_code), paging)
    names = _sees_names(ctx)
    return Page(
        items=[AccountOut.of(r, names=names) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _account(ctx: SiteContext, account_id: uuid.UUID) -> svc.AccountRow:
    row = await svc.account_row(ctx.session, account_id)
    if row is None:
        raise NotFoundError("Cari hesap bulunamadı.")
    _ensure_can_see(ctx, row.account)
    return row


# --- Ekstre -------------------------------------------------------------------------


class EntryOut(BaseModel):
    id: uuid.UUID
    date: dt.date
    due_date: dt.date | None
    debit: Money
    credit: Money
    source: LedgerSource
    source_id: uuid.UUID | None
    description: str
    running_balance: Money = Field(description="bu hareketten sonraki bakiye")


class LastChargeOut(BaseModel):
    period: str
    amount: Money
    lines: list[LineOut]


class StatementOut(BaseModel):
    account: AccountOut
    entries: Page[EntryOut] = Field(description="en yeni üstte; ilk satırın bakiyesi = güncel")
    last_charge: LastChargeOut | None = Field(description="son tahakkukun kalem dökümü")


@router.get("/accounts/{account_id}/statement", summary="Cari ekstre")
async def account_statement(
    account_id: uuid.UUID, ctx: SiteContextDep, paging: Paging
) -> StatementOut:
    row = await _account(ctx, account_id)
    own = ctx.access.person_id == row.account.person_id
    lines, total = await svc.statement(
        ctx.session, account_id, offset=paging.offset, limit=paging.page_size
    )
    last = await svc.last_charge(ctx.session, account_id)
    return StatementOut(
        account=AccountOut.of(row, names=own or _sees_names(ctx)),
        entries=Page(
            items=[
                EntryOut(
                    id=line.entry.id,
                    date=line.entry.date,
                    due_date=line.entry.due_date,
                    debit=line.entry.debit,
                    credit=line.entry.credit,
                    source=LedgerSource(line.entry.source),
                    source_id=line.entry.source_id,
                    description=line.entry.description,
                    running_balance=line.running_balance,
                )
                for line in lines
            ],
            page=paging.page,
            page_size=paging.page_size,
            total=total,
        ),
        last_charge=LastChargeOut(
            period=last.period.name,
            amount=last.charge.amount,
            lines=[LineOut.of(line) for line in last.lines],
        )
        if last
        else None,
    )


# --- Borçlular ----------------------------------------------------------------------


class DebtorOut(AccountOut):
    overdue_days: int = Field(description="bugün − en eski açık vade (en az 0)")


class DebtorSummaryOut(BaseModel):
    total_balance: Money
    debtor_count: int
    over_30_days: Money = Field(description="en eski açık vadesi 30 günü aşan hesapların bakiyesi")
    over_30_count: int
    over_60_days: Money
    over_60_count: int
    average_balance: Money


class DebtorPage(Page[DebtorOut]):
    summary: DebtorSummaryOut


@router.get("/debtors", summary="Borçlular (büyükten küçüğe) ve özet")
async def list_debtors(
    ctx: FinanceRead,
    paging: Paging,
    today: TodayDep,
    q: Annotated[str | None, Query(max_length=60)] = None,
) -> DebtorPage:
    rows, total = await _page_rows(ctx, svc.debtors_query(q), paging)
    summary = await svc.debtor_summary(ctx.session, today)
    names = _sees_names(ctx)
    return DebtorPage(
        items=[
            DebtorOut(
                **AccountOut.of(r, names=names).model_dump(),
                overdue_days=svc.overdue_days(r.oldest_open_due_date, today),
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
        summary=DebtorSummaryOut(
            total_balance=summary.total_balance,
            debtor_count=summary.debtor_count,
            over_30_days=summary.over_30_days,
            over_30_count=summary.over_30_count,
            over_60_days=summary.over_60_days,
            over_60_count=summary.over_60_count,
            average_balance=summary.average_balance,
        ),
    )


# --- Tahsilat -----------------------------------------------------------------------


class PaymentOut(BaseModel):
    id: uuid.UUID
    ledger_account_id: uuid.UUID
    amount: Money
    date: dt.date
    method: PaymentMethod
    reference: str | None
    note: str | None
    status: PaymentStatus
    cash_account_id: uuid.UUID | None
    created_by_name: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, payment: Payment) -> PaymentOut:
        return cls(
            id=payment.id,
            ledger_account_id=payment.ledger_account_id,
            amount=payment.amount,
            date=payment.date,
            method=PaymentMethod(payment.method),
            reference=payment.reference,
            note=payment.note,
            status=PaymentStatus(payment.status),
            cash_account_id=payment.cash_account_id,
            created_by_name=payment.created_by_name,
            created_at=payment.created_at,
        )


class PaymentIn(BaseModel):
    ledger_account_id: uuid.UUID
    amount: Money
    date: dt.date
    method: PaymentMethod
    reference: str | None = Field(default=None, max_length=100, description="boşsa referans kodu")
    note: str | None = Field(default=None, max_length=500)
    cash_account_id: uuid.UUID | None = Field(
        default=None, description="paranın girdiği kasa/banka hesabı (kasaya giriş hareketi)"
    )


class PaymentResultOut(BaseModel):
    payment: PaymentOut
    applied: Money = Field(description="borçlara mahsup edilen")
    unapplied: Money = Field(description="avans olarak kalan")
    closed_debt_count: int
    balance: Money = Field(description="tahsilattan sonraki bakiye")


def _payment_message(row: svc.AccountRow, amount: Decimal, closed: int, unapplied: Decimal) -> str:
    """`A-12 — Ayşe YILMAZ: 1.000,00 TL tahsilat kaydedildi, 1 borç kaydına mahsup edildi.`"""
    head = f"{row.unit_name} — {row.person_name}: {format_money_tr(amount)} tahsilat kaydedildi"
    if closed == 0:
        return f"{head}, tamamı avans olarak kaldı."
    text = f"{head}, {closed} borç kaydına mahsup edildi"
    if unapplied > ZERO:
        text += f", {format_money_tr(unapplied)} avans olarak kaldı"
    return text + "."


@router.post(
    "/payments",
    status_code=status.HTTP_201_CREATED,
    summary="Tahsilat kaydet",
    response_model=Written[PaymentResultOut],
)
async def record_payment(
    ctx: PaymentRecord,
    body: PaymentIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[PaymentResultOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        row = await svc.account_row(ctx.session, body.ledger_account_id)
        if row is None:  # başka sitenin hesabı da burada "yok"tur
            raise FinanceRuleError(
                "account_not_found", "Cari hesap bulunamadı.", field="ledger_account_id"
            )
        recorded = await payment_svc.record_payment(
            ctx.session,
            row.account,
            amount=body.amount,
            day=body.date,
            method=body.method,
            reference=body.reference,
            note=body.note,
            today=today,
            recorded_by=current.user.full_name,
            cash_account_id=body.cash_account_id,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = recorded.result
    written = Written(
        data=PaymentResultOut(
            payment=PaymentOut.of(recorded.payment),
            applied=result.applied,
            unapplied=result.unapplied,
            closed_debt_count=result.closed_debt_count,
            balance=recorded.balance,
        ),
        message=_payment_message(row, body.amount, result.closed_debt_count, result.unapplied),
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, written)
    return written


class PaymentListItem(PaymentOut):
    reference_code: str
    unit_name: str
    person_name: str | None


@router.get("/payments", summary="Tahsilatlar (en yeni üstte)")
async def list_payments(
    ctx: FinanceRead,
    paging: Paging,
    account_id: uuid.UUID | None = None,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> Page[PaymentListItem]:
    query = svc.payments_query(account_id=account_id, date_from=date_from, date_to=date_to)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    payments = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    ids = {p.ledger_account_id for p in payments}
    accounts = {
        r.account.id: r
        for r in (
            svc.to_row(tuple(values))
            for values in await ctx.session.execute(
                svc.accounts_query().where(LedgerAccount.id.in_(ids))
            )
        )
    }
    names = _sees_names(ctx)
    return Page(
        items=[
            PaymentListItem(
                **PaymentOut.of(p).model_dump(),
                reference_code=accounts[p.ledger_account_id].account.reference_code,
                unit_name=accounts[p.ledger_account_id].unit_name,
                person_name=accounts[p.ledger_account_id].person_name if names else None,
            )
            for p in payments
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class AllocationOut(BaseModel):
    ledger_entry_id: uuid.UUID
    description: str = Field(description="kapatılan borç: `06/2026 tahakkuku — Aidat`")
    due_date: dt.date | None
    amount: Money


class SiteRef(BaseModel):
    name: str
    address: str | None
    city: str | None
    district: str | None


class ReceiptOut(BaseModel):
    """Makbuz basmak için gereken veri. Numaralandırma ve yasal biçim açık karar (K15)."""

    receipt_number: None = Field(default=None, description="K15 kararına kadar null")
    site: SiteRef
    payment: PaymentOut
    account: AccountOut
    allocations: list[AllocationOut]
    applied: Money
    unapplied: Money = Field(description="avans olarak kalan")


@router.get("/payments/{payment_id}", summary="Tahsilat ayrıntısı (makbuz verisi)")
async def get_payment(payment_id: uuid.UUID, ctx: SiteContextDep) -> ReceiptOut:
    payment = await svc.get_payment(ctx.session, payment_id)
    if payment is None:
        raise NotFoundError("Tahsilat bulunamadı.")
    row = await _account(ctx, payment.ledger_account_id)
    allocations = await svc.payment_allocations(ctx.session, payment.id)
    applied = sum((a.amount for a in allocations), ZERO)
    site = ctx.site
    return ReceiptOut(
        site=SiteRef(name=site.name, address=site.address, city=site.city, district=site.district),
        payment=PaymentOut.of(payment),
        account=AccountOut.of(
            row, names=ctx.access.person_id == row.account.person_id or _sees_names(ctx)
        ),
        allocations=[
            AllocationOut(
                ledger_entry_id=a.ledger_entry_id,
                description=a.description,
                due_date=a.due_date,
                amount=a.amount,
            )
            for a in allocations
        ],
        applied=applied,
        unapplied=payment.amount - applied,
    )
