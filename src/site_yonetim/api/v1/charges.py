"""Tahakkuk koşuları — docs/06 §2.6, docs/04 §4, §7.

- Önizleme hiçbir şey yazmaz; varsayılan dönem "sıradaki dönem"dir.
- Kaydetme ve ters kayıt para yazar: `Idempotency-Key` kabul eder, tekrar gönderimde ilk
  yanıt döner. Aynı dönem ikinci kez kesilemez (409).
- Silme ve düzenleme yok; yanlış koşu ters kayıtla iptal edilir, orijinal yerinde kalır.
"""

import uuid
from collections import defaultdict
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    ChargePost,
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.charging.engine import PreviewCharge, PreviewLine, WarningKind
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import AllocationKind, ChargeRunStatus, FinanceRuleError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Block,
    Charge,
    ChargeLine,
    ChargeRun,
    LedgerAccount,
    Period,
    Person,
    Unit,
)
from site_yonetim.services import charging as svc

router = APIRouter(prefix="/sites/{slug}/charge-runs", tags=["tahakkuk"])
Paging = Annotated[PageParams, Depends()]


class LineOut(BaseModel):
    budget_item_id: uuid.UUID
    description: str
    amount: Money
    allocation_kind: AllocationKind
    weight: str | None = Field(description="bu bölümün ağırlığı (ör. 110 m²), metin")
    weight_total: str | None = Field(description="kapsamdaki toplam ağırlık, metin")
    source_amount: Money | None = Field(description="dağıtılan havuz")
    explanation: str | None = Field(description="`Brüt 110,00 m² / 2.140,00 m² × 50.000,00 TL`")

    @classmethod
    def of(cls, line: PreviewLine | ChargeLine) -> LineOut:
        def text(value: Any) -> str | None:
            return None if value is None else format(value.normalize(), "f")

        return cls(
            budget_item_id=line.budget_item_id,
            description=line.description,
            amount=line.amount,
            allocation_kind=AllocationKind(line.allocation_kind),
            weight=text(line.weight),
            weight_total=text(line.weight_total),
            source_amount=line.source_amount,
            explanation=line.explanation,
        )


class ChargeOut(BaseModel):
    unit_id: uuid.UUID
    unit_name: str = Field(description="`A-12`")
    ledger_account_id: uuid.UUID
    reference_code: str
    person_id: uuid.UUID
    person_name: str
    account_kind: AccountKind = Field(description="occupant: oturan hesabı · owner: malik hesabı")
    amount: Money
    lines: list[LineOut]

    @classmethod
    def of(cls, charge: PreviewCharge) -> ChargeOut:
        return cls(
            unit_id=charge.unit_id,
            unit_name=charge.unit_name,
            ledger_account_id=charge.ledger_account_id,
            reference_code=charge.reference_code,
            person_id=charge.person_id,
            person_name=charge.person_name,
            account_kind=charge.account_kind,
            amount=charge.amount,
            lines=[LineOut.of(line) for line in charge.lines],
        )


class WarningOut(BaseModel):
    kind: WarningKind
    message: str
    unit_id: uuid.UUID | None
    budget_item_id: uuid.UUID | None


class ItemTotalOut(BaseModel):
    budget_item_id: uuid.UUID
    name: str
    amount: Money = Field(description="kalemin bu dönemdeki tutarı")
    distributed: Money = Field(description="bölümlere dağıtılan; `amount`'a eşit olmalı")


class PlanRef(BaseModel):
    id: uuid.UUID
    fiscal_year: int
    name: str


class PreviewOut(BaseModel):
    period: str = Field(description="`09/2026`")
    year: int
    month: int
    charge_date: date
    due_date: date
    already_charged: bool = Field(description="bu dönemin geçerli koşusu var; kaydetmek 409 döner")
    budget_plan: PlanRef
    total_amount: Money
    unit_count: int = Field(description="borç yazılan farklı bölüm sayısı")
    charge_count: int
    warnings: list[WarningOut]
    item_totals: list[ItemTotalOut]
    not_due_items: list[str] = Field(description="takvim gereği bu dönem kesilmeyen kalemler")
    charges: Page[ChargeOut]


@router.get("/preview", summary="Tahakkuk önizlemesi (hiçbir şey yazılmaz)")
async def preview(
    ctx: ChargePost,
    today: TodayDep,
    paging: Paging,
    charge_date: Annotated[date | None, Query(description="boşsa sıradaki dönemin 1'i")] = None,
    due_date: Annotated[date | None, Query(description="boşsa tahakkuk + 14 gün")] = None,
) -> PreviewOut:
    try:
        prepared = await svc.prepare(ctx.session, today, charge_date=charge_date, due_date=due_date)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = prepared.preview
    period_row = await ctx.session.scalar(
        select(Period.id).where(
            Period.year == prepared.period.year, Period.month == prepared.period.month
        )
    )
    already = period_row is not None and await svc.valid_run_of(ctx.session, period_row) is not None
    page = result.charges[paging.offset : paging.offset + paging.page_size]
    return PreviewOut(
        period=prepared.period.name,
        year=prepared.period.year,
        month=prepared.period.month,
        charge_date=prepared.charge_date,
        due_date=prepared.due_date,
        already_charged=already,
        budget_plan=PlanRef(
            id=prepared.plan.id, fiscal_year=prepared.plan.fiscal_year, name=prepared.plan.name
        ),
        total_amount=result.total_amount,
        unit_count=result.unit_count,
        charge_count=len(result.charges),
        warnings=[
            WarningOut(
                kind=w.kind, message=w.message, unit_id=w.unit_id, budget_item_id=w.budget_item_id
            )
            for w in result.warnings
        ],
        item_totals=[
            ItemTotalOut(
                budget_item_id=t.budget_item_id,
                name=t.name,
                amount=t.amount,
                distributed=t.distributed,
            )
            for t in result.item_totals
        ],
        not_due_items=prepared.not_due_items,
        charges=Page(
            items=[ChargeOut.of(c) for c in page],
            page=paging.page,
            page_size=paging.page_size,
            total=len(result.charges),
        ),
    )


# --- Koşular ------------------------------------------------------------------------


class ChargeRunOut(BaseModel):
    id: uuid.UUID
    period: str
    year: int
    month: int
    status: ChargeRunStatus
    charge_date: date
    due_date: date
    posted_at: datetime | None
    posted_by: str | None
    reversal_of_run_id: uuid.UUID | None = Field(description="ters kayıt koşusuysa orijinali")
    reason: str | None = Field(description="ters kayıt gerekçesi")
    total_amount: Money = Field(description="koşunun borç toplamı (ters kayıtta orijinalinki)")
    charge_count: int


async def _runs_out(ctx: SiteContext, runs: list[ChargeRun]) -> list[ChargeRunOut]:
    """Toplamlar veritabanında; sayfa başına sabit sorgu."""
    session = ctx.session
    source_ids = {r.reversal_of_run_id or r.id for r in runs}
    totals = {
        run_id: (amount, count)
        for run_id, amount, count in await session.execute(
            select(Charge.charge_run_id, func.sum(Charge.amount), func.count())
            .where(Charge.charge_run_id.in_(source_ids))
            .group_by(Charge.charge_run_id)
        )
    }
    periods = {
        row.id: YearMonth(row.year, row.month)
        for row in await session.execute(
            select(Period.id, Period.year, Period.month).where(
                Period.id.in_({r.period_id for r in runs})
            )
        )
    }
    out = []
    for run in runs:
        amount, count = totals.get(run.reversal_of_run_id or run.id, (ZERO, 0))
        period = periods[run.period_id]
        out.append(
            ChargeRunOut(
                id=run.id,
                period=period.name,
                year=period.year,
                month=period.month,
                status=ChargeRunStatus(run.status),
                charge_date=run.charge_date,
                due_date=run.due_date,
                posted_at=run.posted_at,
                posted_by=run.posted_by,
                reversal_of_run_id=run.reversal_of_run_id,
                reason=run.reason,
                total_amount=amount,
                charge_count=count,
            )
        )
    return out


@router.get("", summary="Tahakkuk koşuları (en yeni üstte)")
async def list_runs(ctx: FinanceRead, paging: Paging) -> Page[ChargeRunOut]:
    total = await ctx.session.scalar(select(func.count()).select_from(ChargeRun)) or 0
    runs = list(
        await ctx.session.scalars(
            select(ChargeRun)
            .order_by(ChargeRun.created_at.desc(), ChargeRun.id.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    return Page(
        items=await _runs_out(ctx, runs), page=paging.page, page_size=paging.page_size, total=total
    )


async def _run(ctx: SiteContext, run_id: uuid.UUID) -> ChargeRun:
    run = await svc.get_run(ctx.session, run_id)
    if run is None:
        raise NotFoundError("Tahakkuk koşusu bulunamadı.")
    return run


@router.get("/{run_id}", summary="Koşu ayrıntısı")
async def get_run(run_id: uuid.UUID, ctx: FinanceRead) -> ChargeRunOut:
    [out] = await _runs_out(ctx, [await _run(ctx, run_id)])
    return out


class PostedChargeOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    ledger_account_id: uuid.UUID
    reference_code: str
    person_name: str
    account_kind: AccountKind
    amount: Money
    lines: list[LineOut]


@router.get("/{run_id}/charges", summary="Koşunun borçları ve kalem dökümü (sayfalı)")
async def run_charges(run_id: uuid.UUID, ctx: FinanceRead, paging: Paging) -> Page[PostedChargeOut]:
    run = await _run(ctx, run_id)
    session = ctx.session
    base = select(Charge).where(Charge.charge_run_id == (run.reversal_of_run_id or run.id))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        await session.execute(
            select(
                Charge, Unit.number, Block.name, LedgerAccount, Person.first_name, Person.last_name
            )
            .join(Unit, and_(Unit.id == Charge.unit_id, Unit.site_id == Charge.site_id))
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .join(
                LedgerAccount,
                and_(
                    LedgerAccount.id == Charge.ledger_account_id,
                    LedgerAccount.site_id == Charge.site_id,
                ),
            )
            .join(
                Person,
                and_(Person.id == LedgerAccount.person_id, Person.site_id == LedgerAccount.site_id),
            )
            .where(Charge.charge_run_id == (run.reversal_of_run_id or run.id))
            .order_by(
                Block.sort_order,
                Block.name,
                func.length(Unit.number),
                Unit.number,
                LedgerAccount.kind.desc(),
            )
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    lines: dict[uuid.UUID, list[ChargeLine]] = defaultdict(list)
    for line in await session.scalars(
        select(ChargeLine)
        .where(ChargeLine.charge_id.in_([row[0].id for row in rows]))
        .order_by(ChargeLine.id)
    ):
        lines[line.charge_id].append(line)
    items = [
        PostedChargeOut(
            id=charge.id,
            unit_id=charge.unit_id,
            unit_name=f"{block}-{number}" if block else number,
            ledger_account_id=account.id,
            reference_code=account.reference_code,
            person_name=f"{first} {last}",
            account_kind=AccountKind(account.kind),
            amount=charge.amount,
            lines=[LineOut.of(line) for line in lines[charge.id]],
        )
        for charge, number, block, account, first, last in rows
    ]
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


class PostIn(BaseModel):
    charge_date: date | None = Field(default=None, description="boşsa sıradaki dönemin 1'i")
    due_date: date | None = Field(default=None, description="boşsa tahakkuk + 14 gün")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Tahakkuku kaydet",
    response_model=Written[ChargeRunOut],
    responses={409: {"description": "Dönem zaten kesilmiş / kesinleşmiş proje yok"}},
)
async def post_run(
    ctx: ChargePost,
    body: PostIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ChargeRunOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        prepared = await svc.prepare(
            ctx.session, today, charge_date=body.charge_date, due_date=body.due_date
        )
        posted = await svc.post(ctx.session, prepared, posted_by=current.user.full_name, now=now)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [run] = await _runs_out(ctx, [posted.run])
    result = Written(
        data=run,
        message=f"{posted.period.name} tahakkuku kesildi: {posted.unit_count} bölüm, "
        f"{format_money_tr(posted.total_amount)}.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, result)
    return result


class ReverseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="ters kayıt gerekçesi")


@router.post(
    "/{run_id}/reverse",
    summary="Koşuyu ters kayıtla iptal et",
    response_model=Written[ChargeRunOut],
    responses={409: {"description": "Zaten ters kaydedilmiş / ters kaydedilemez"}},
)
async def reverse_run(
    run_id: uuid.UUID,
    ctx: ChargePost,
    body: ReverseIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ChargeRunOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        run = await _run(ctx, run_id)
        reversal = await svc.reverse(
            ctx.session,
            run,
            reason=" ".join(body.reason.split()),
            today=today,
            reversed_by=current.user.full_name,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _runs_out(ctx, [reversal])
    result = Written(
        data=out,
        message=f"{out.period} tahakkuku ters kayıtla iptal edildi; dönem yeniden kesilebilir.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result
