"""Cari hesap işlemleri — frontend servis istekleri 02 (devir bakiye, issue #25).

Para yazar: `Idempotency-Key` kabul eder; tekrar gönderimde ilk yanıt döner. İşlem denetim
kaydına tek satır olarak yazılır (defter satırları türetilmiş sayılır, `models/audit.py`).
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from site_yonetim.api.deps import NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import (
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import accounts as account_svc
from site_yonetim.services import opening_balances as opening_svc
from site_yonetim.services.opening_balances import OpeningDirection

router = APIRouter(prefix="/sites/{slug}", tags=["tahsilat ve cari"])
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]


class OpeningBalanceIn(BaseModel):
    amount: Money
    direction: OpeningDirection = Field(description="debit: sakin borçlu · credit: alacaklı")
    date: dt.date = Field(description="genelde sisteme geçiş tarihi; gelecekte olamaz")
    description: str | None = Field(default=None, max_length=200)


class OpeningBalanceOut(BaseModel):
    id: uuid.UUID = Field(description="defter hareketi (ekstrede `source: opening`)")
    amount: Money
    direction: OpeningDirection
    date: dt.date
    description: str | None


@router.post(
    "/accounts/{account_id}/opening-balance",
    status_code=status.HTTP_201_CREATED,
    summary="Devir bakiye (hesap başına bir kez)",
    response_model=Written[OpeningBalanceOut],
)
async def record_opening_balance(
    account_id: uuid.UUID,
    body: OpeningBalanceIn,
    ctx: PaymentRecord,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[OpeningBalanceOut] | JSONResponse:
    """İkinci deneme 409 `already_exists`; tutar ≤ 0 ya da ileri tarih 422."""
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    row = await account_svc.account_row(ctx.session, account_id)
    if row is None:  # başka sitenin hesabı da burada "yok"tur
        raise NotFoundError("Hesap bulunamadı.")
    note = " ".join(body.description.split()) or None if body.description else None
    try:
        entry = await opening_svc.record(
            ctx.session,
            row.account,
            amount=body.amount,
            direction=body.direction,
            day=body.date,
            note=note,
            today=today,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    data = OpeningBalanceOut(
        id=entry.id,
        amount=body.amount,
        direction=body.direction,
        date=body.date,
        description=note,
    )
    detail = data.model_dump(mode="json", exclude={"id"})
    record(ctx.session, AuditAction.CREATE, "opening_balances", entry.id,
           {"ledger_account_id": row.account.id, **detail})  # fmt: skip
    side = "borç" if body.direction is OpeningDirection.DEBIT else "alacak"
    written = Written(
        data=data, message=f"{format_money_tr(body.amount)} devir {side} olarak işlendi."
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, written)
    return written
