"""Kasa ve banka uçları — docs/06 §2.8, docs/04 §10.

Okuma `finance.cash.read`, yazma `finance.cash.manage`. Para yazan uçlar `Idempotency-Key`
kabul eder. Hareket silinmez, düzenlenmez: yanlış hareket ters hareketle geri alınır;
tahsilat/gider kaynaklı hareket burada geri alınamaz (409).
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.cash import CashAccountKind, CashSource, Direction
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import CashAccount, CashMovement
from site_yonetim.services import cash as svc

router = APIRouter(prefix="/sites/{slug}", tags=["kasa ve banka"])
CashRead = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_CASH_READ))]
CashManage = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_CASH_MANAGE))]
Paging = Annotated[PageParams, Depends()]


class CashAccountOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: CashAccountKind
    bank_name: str | None
    iban: str | None
    opening_balance: Money
    opening_date: dt.date | None
    is_active: bool
    note: str | None
    inflow: Money = Field(description="toplam giren")
    outflow: Money = Field(description="toplam çıkan")
    balance: Money = Field(description="eksi olabilir — ekranda uyarı")

    @classmethod
    def of(cls, row: svc.AccountWithBalance) -> CashAccountOut:
        a = row.account
        return cls(
            id=a.id,
            name=a.name,
            kind=CashAccountKind(a.kind),
            bank_name=a.bank_name,
            iban=a.iban,
            opening_balance=a.opening_balance,
            opening_date=a.opening_date,
            is_active=a.is_active,
            note=a.note,
            inflow=row.inflow,
            outflow=row.outflow,
            balance=row.balance,
        )


class CashAccountsOut(BaseModel):
    items: list[CashAccountOut]
    total_balance: Money = Field(description="yalnız aktif hesapların toplamı")


@router.get("/cash-accounts", summary="Kasa/banka hesapları ve bakiyeleri")
async def list_accounts(ctx: CashRead) -> CashAccountsOut:
    rows = await svc.accounts_with_balances(ctx.session)
    return CashAccountsOut(
        items=[CashAccountOut.of(r) for r in rows],
        total_balance=sum((r.balance for r in rows if r.account.is_active), ZERO),
    )


class CashAccountIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    kind: CashAccountKind
    bank_name: str | None = Field(default=None, max_length=100)
    iban: str | None = Field(default=None, max_length=50)
    opening_balance: Money = Decimal("0.00")
    opening_date: dt.date | None = None
    note: str | None = Field(default=None, max_length=500)


async def _account_out(ctx: SiteContext, account_id: uuid.UUID) -> CashAccountOut:
    rows = await svc.accounts_with_balances(ctx.session)
    return CashAccountOut.of(next(r for r in rows if r.account.id == account_id))


@router.post(
    "/cash-accounts",
    status_code=status.HTTP_201_CREATED,
    summary="Hesap aç",
    response_model=Written[CashAccountOut],
)
async def create_account(
    ctx: CashManage,
    body: CashAccountIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[CashAccountOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        account = await svc.create_account(
            ctx.session,
            svc.NewAccount(
                name=body.name,
                kind=body.kind,
                bank_name=body.bank_name,
                iban=body.iban,
                opening_balance=body.opening_balance,
                opening_date=body.opening_date,
                note=body.note,
            ),
            today=today,
            created_by=current.user.full_name,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = Written(
        data=await _account_out(ctx, account.id), message=f"'{account.name}' hesabı açıldı."
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, result)
    return result


# --- Ekstre ---------------------------------------------------------------------------


class MovementOut(BaseModel):
    id: uuid.UUID
    cash_account_id: uuid.UUID
    date: dt.date
    inflow: Money
    outflow: Money
    description: str
    reference: str | None
    source: CashSource
    source_id: uuid.UUID | None
    reversal_of_id: uuid.UUID | None
    is_reversed: bool = Field(description="geri alındı etiketi")
    created_by_name: str | None
    running_balance: Money | None = Field(description="ekstrede: bu hareketten sonraki bakiye")

    @classmethod
    def of(
        cls, m: CashMovement, *, running: Decimal | None = None, is_reversed: bool = False
    ) -> MovementOut:
        return cls(
            id=m.id,
            cash_account_id=m.cash_account_id,
            date=m.date,
            inflow=m.inflow,
            outflow=m.outflow,
            description=m.description,
            reference=m.reference,
            source=CashSource(m.source),
            source_id=m.source_id,
            reversal_of_id=m.reversal_of_id,
            is_reversed=is_reversed,
            created_by_name=m.created_by_name,
            running_balance=running,
        )


class StatementOut(BaseModel):
    account: CashAccountOut
    opening: Money = Field(description="devreden: başlangıçtan önceki hareketlerin neti")
    closing: Money
    total_in: Money
    total_out: Money
    movements: Page[MovementOut] = Field(description="en yeni üstte; yürüyen bakiye tüm aralıktan")


async def _account(ctx: SiteContext, account_id: uuid.UUID) -> CashAccount:
    account = await svc.get_account(ctx.session, account_id)
    if account is None:
        raise NotFoundError("Kasa/banka hesabı bulunamadı.")
    return account


@router.get("/cash-accounts/{account_id}/statement", summary="Hesap ekstresi")
async def account_statement(
    account_id: uuid.UUID,
    ctx: CashRead,
    paging: Paging,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> StatementOut:
    account = await _account(ctx, account_id)
    statement = await svc.statement(
        ctx.session, account.id, date_from=date_from, date_to=date_to,
        offset=paging.offset, limit=paging.page_size,
    )  # fmt: skip
    return StatementOut(
        account=await _account_out(ctx, account.id),
        opening=statement.opening,
        closing=statement.closing,
        total_in=statement.total_in,
        total_out=statement.total_out,
        movements=Page(
            items=[
                MovementOut.of(
                    line.movement, running=line.running_balance, is_reversed=line.is_reversed
                )
                for line in statement.lines
            ],
            page=paging.page,
            page_size=paging.page_size,
            total=statement.count,
        ),
    )


# --- Elle hareket, aktarım, geri alma --------------------------------------------------


class MovementIn(BaseModel):
    cash_account_id: uuid.UUID
    date: dt.date
    direction: Direction = Field(description="in: giriş (faiz, kira geliri) · out: çıkış (masraf)")
    amount: Money
    description: str = Field(min_length=3, max_length=200)
    reference: str | None = Field(default=None, max_length=100)


@router.post(
    "/cash-movements",
    status_code=status.HTTP_201_CREATED,
    summary="Elle hareket",
    response_model=Written[MovementOut],
)
async def manual_movement(
    ctx: CashManage,
    body: MovementIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[MovementOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        movement = await svc.manual_movement(
            ctx.session, account_id=body.cash_account_id, day=body.date,
            direction=body.direction, amount=body.amount, description=body.description,
            reference=body.reference, today=today, created_by=current.user.full_name,
        )  # fmt: skip
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    label = "giriş" if body.direction is Direction.IN else "çıkış"
    result = Written(
        data=MovementOut.of(movement), message=f"{format_money_tr(body.amount)} {label} kaydedildi."
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, result)
    return result


class TransferIn(BaseModel):
    from_id: uuid.UUID
    to_id: uuid.UUID
    date: dt.date
    amount: Money
    note: str | None = Field(default=None, max_length=200)


class TransferOut(BaseModel):
    outgoing: MovementOut
    incoming: MovementOut


@router.post(
    "/cash-transfers",
    status_code=status.HTTP_201_CREATED,
    summary="Hesaplar arası aktarım",
    response_model=Written[TransferOut],
)
async def transfer(
    ctx: CashManage,
    body: TransferIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[TransferOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        out, incoming = await svc.transfer(
            ctx.session, from_id=body.from_id, to_id=body.to_id, day=body.date,
            amount=body.amount, note=body.note, today=today, created_by=current.user.full_name,
        )  # fmt: skip
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = Written(
        data=TransferOut(outgoing=MovementOut.of(out), incoming=MovementOut.of(incoming)),
        message=f"{format_money_tr(body.amount)} aktarıldı.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, result)
    return result


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post(
    "/cash-movements/{movement_id}/reverse",
    summary="Hareketi geri al (ters hareket)",
    response_model=Written[MovementOut],
)
async def reverse_movement(
    movement_id: uuid.UUID,
    ctx: CashManage,
    body: ReasonIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[MovementOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        movement = await svc.get_movement(ctx.session, movement_id)
        if movement is None:
            raise NotFoundError("Kasa hareketi bulunamadı.")
        reversal = await svc.reverse_movement(
            ctx.session,
            movement,
            reason=body.reason,
            today=today,
            created_by=current.user.full_name,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = Written(data=MovementOut.of(reversal), message="Hareket ters kayıtla geri alındı.")
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result
