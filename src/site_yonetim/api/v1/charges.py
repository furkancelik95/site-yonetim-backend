"""Tahakkuk koşuları — docs/06 §2.6, docs/04 §4, §7.

- Önizleme hiçbir şey yazmaz; varsayılan dönem "sıradaki dönem"dir. Motor finans
  parametrelerini okur: kaynak (K23), kıst (K9), vade günü (K25).
- Kaydetme ve ters kayıt para yazar: `Idempotency-Key` kabul eder, tekrar gönderimde ilk
  yanıt döner. Aynı dönem ikinci kez kesilemez (409).
- FIN-05 / K21: "Tahakkuk" onay kuralı varsa kaydetme taslağı onaya gönderir (202); onaylanınca
  tahakkuk tarihinde kesinleşir. Kural yoksa bugünkü gibi doğrudan kesilir (201). Taslak ayrıca
  hazırlanıp (`/drafts`) incelenebilir, yenilenebilir, gönderilebilir, iptal edilebilir.
- Silme ve düzenleme yok; yanlış koşu ters kayıtla iptal edilir, orijinal yerinde kalır.
  Kesinleşmiş tahakkukta tek satır "Tahakkuk düzeltme" ile düzeltilir (D-10).
"""

import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import ApprovalPendingOut, Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    ChargePost,
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.charging import snapshot
from site_yonetim.domain.charging.engine import (
    ItemSource,
    PreviewCharge,
    PreviewLine,
    WarningKind,
)
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import (
    AllocationKind,
    ChargeRunStatus,
    FinanceRuleError,
)
from site_yonetim.domain.finance_params import ChargeSource, Proration
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Block,
    Charge,
    ChargeAdjustment,
    ChargeLine,
    ChargeRun,
    ChargeRunDraft,
    LedgerAccount,
    Period,
    Person,
    Unit,
)
from site_yonetim.services import charge_adjustments, charge_approvals, charge_diff, charge_drafts
from site_yonetim.services import charging as svc
from site_yonetim.services.approvals import ApprovalError
from site_yonetim.services.approvals import Person as Requester

router = APIRouter(prefix="/sites/{slug}/charge-runs", tags=["tahakkuk"])
Paging = Annotated[PageParams, Depends()]


class LineOut(BaseModel):
    id: uuid.UUID | None = Field(default=None, description="kesinleşmiş satır; önizlemede null")
    budget_item_id: uuid.UUID | None = Field(description="işletme projesi kalemi")
    billing_rule_version_id: uuid.UUID | None = Field(
        default=None, description="aidat kuralı sürümü (K23)"
    )
    description: str
    amount: Money
    current_amount: Money | None = Field(
        default=None, description="satır düzeltmesinden sonraki tutar; düzeltilmediyse null"
    )
    allocation_kind: AllocationKind
    weight: str | None = Field(description="bu bölümün ağırlığı (ör. 110 m²), metin")
    weight_total: str | None = Field(description="kapsamdaki toplam ağırlık, metin")
    source_amount: Money | None = Field(description="dağıtılan havuz")
    explanation: str | None = Field(description="`Brüt 110,00 m² / 2.140,00 m² × 50.000,00 TL`")

    @classmethod
    def of(cls, line: PreviewLine | ChargeLine, current: Decimal | None = None) -> LineOut:
        def text(value: Any) -> str | None:
            return None if value is None else format(value.normalize(), "f")

        if isinstance(line, PreviewLine):
            rule = line.source is ItemSource.BILLING_RULE
            ids = (None if rule else line.budget_item_id, line.budget_item_id if rule else None)
            line_id = None
        else:
            ids = (line.budget_item_id, line.billing_rule_version_id)
            line_id = line.id
        return cls(
            id=line_id,
            budget_item_id=ids[0],
            billing_rule_version_id=ids[1],
            current_amount=current,
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
    days: int | None = Field(
        default=None, description="K9 kıst: ödeyenin dönemdeki gün sayısı (tam dönemde null)"
    )
    period_days: int | None = Field(default=None, description="dönemin gün sayısı (kıstta)")
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
            days=charge.days,
            period_days=charge.period_days,
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
    distributed: Money = Field(description="bölümlere dağıtılan; `amount − exempted`'e eşit olmalı")
    exempted: Money = Field(default=Decimal("0.00"), description="muafiyetle yazılmayan")


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
    source: ChargeSource = Field(description="K23 tahakkuk kaynağı (finans parametresi)")
    proration: Proration = Field(description="K9 kıst yöntemi (finans parametresi)")
    approval_required: bool = Field(
        description="'Tahakkuk' onay kuralı var: kaydetmek taslağı onaya gönderir (202)"
    )
    open_draft_id: uuid.UUID | None = Field(description="bu dönemin açık taslağı")
    budget_plan: PlanRef | None = Field(description="aidat kuralı kaynağında null")
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
    draft = await charge_drafts.open_draft_of(ctx.session, period_row) if period_row else None
    page = result.charges[paging.offset : paging.offset + paging.page_size]
    return PreviewOut(
        period=prepared.period.name,
        year=prepared.period.year,
        month=prepared.period.month,
        charge_date=prepared.charge_date,
        due_date=prepared.due_date,
        already_charged=already,
        source=prepared.source,
        proration=prepared.proration,
        approval_required=await charge_drafts.rule_exists(ctx.session, today),
        open_draft_id=draft.id if draft else None,
        budget_plan=_plan_ref(prepared),
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
                exempted=t.exempted,
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


class ChargeDiffCountsOut(BaseModel):
    increase: int
    decrease: int
    new: int
    missing: int


class ChargeDiffRowOut(BaseModel):
    account_id: uuid.UUID
    unit_name: str
    person_name: str
    account_kind: AccountKind
    previous: Money | None
    current: Money | None
    change_percent: Decimal | None = Field(description="yüzde, 1 ondalık (ör. 12.4)")
    flag: Literal["increase", "decrease", "new", "missing"]


class ChargeDiffOut(BaseModel):
    period: str = Field(description='"11/2026"')
    base_period: str | None = Field(description="karşılaştırılan son kesinleşmiş dönem")
    threshold: int = Field(description="finans parametresi `diff_threshold_percent`")
    compared: int = Field(description="önizlemedeki hesap sayısı")
    counts: ChargeDiffCountsOut
    rows: list[ChargeDiffRowOut]


def _diff_out(report: charge_diff.DiffReport) -> ChargeDiffOut:
    return ChargeDiffOut(
        period=report.period.name,
        base_period=report.base_period.name if report.base_period else None,
        threshold=report.threshold,
        compared=report.compared,
        counts=ChargeDiffCountsOut(**report.counts()),
        rows=[
            ChargeDiffRowOut(
                account_id=r.account_id,
                unit_name=r.unit_name,
                person_name=r.person_name,
                account_kind=AccountKind(r.account_kind),
                previous=r.previous,
                current=r.current,
                change_percent=r.change_percent,
                flag=r.flag,  # type: ignore[arg-type]
            )
            for r in report.rows
        ],
    )


@router.get("/preview/diff", summary="Tahakkuk fark raporu (önizleme ↔ son kesinleşmiş dönem)")
async def preview_diff(
    ctx: FinanceRead,
    today: TodayDep,
    charge_date: Annotated[date | None, Query(description="önizlemeyle aynı")] = None,
    due_date: Annotated[date | None, Query(description="önizlemeyle aynı")] = None,
) -> ChargeDiffOut:
    """Salt okuma; eşik üstü artan/azalan, yeni ve bu dönem olmayan hesaplar (FIN-05)."""
    try:
        prepared = await svc.prepare(ctx.session, today, charge_date=charge_date, due_date=due_date)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    return _diff_out(await charge_diff.report(ctx.session, prepared))


def _plan_ref(prepared: svc.PreparedRun) -> PlanRef | None:
    plan = prepared.plan
    return PlanRef(id=plan.id, fiscal_year=plan.fiscal_year, name=plan.name) if plan else None


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
    total_amount: Money = Field(
        description="koşunun borç toplamı (ters kayıtta orijinalinki, taslakta önizlemeninki)"
    )
    charge_count: int
    source: ChargeSource = Field(description="K23 tahakkuk kaynağı")
    approval_request_id: uuid.UUID | None = Field(description="'Tahakkuk' onay talebi")
    created_by: str | None = Field(description="taslağı hazırlayan")
    warning_count: int = Field(default=0, description="taslak önizlemesindeki uyarı sayısı")


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
    warnings: dict[uuid.UUID, int] = {}
    draft_ids = [r.id for r in runs if r.status not in _POSTED_STATES]
    if draft_ids:
        for row in await session.scalars(
            select(ChargeRunDraft).where(ChargeRunDraft.charge_run_id.in_(draft_ids))
        ):
            preview, _ = snapshot.load(row.snapshot)
            totals[row.charge_run_id] = (preview.total_amount, len(preview.charges))
            warnings[row.charge_run_id] = len(preview.warnings)
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
                source=ChargeSource(run.source),
                approval_request_id=run.approval_request_id,
                created_by=run.created_by,
                warning_count=warnings.get(run.id, 0),
            )
        )
    return out


_POSTED_STATES = frozenset({ChargeRunStatus.POSTED.value, ChargeRunStatus.REVERSED.value})


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
    id: uuid.UUID | None = Field(description="taslakta null")
    unit_id: uuid.UUID
    unit_name: str
    ledger_account_id: uuid.UUID
    reference_code: str
    person_name: str | None = Field(description="people.read izni yoksa null")
    account_kind: AccountKind
    amount: Money
    current_amount: Money | None = Field(
        default=None, description="satır düzeltmelerinden sonraki toplam; düzeltme yoksa null"
    )
    days: int | None = Field(default=None, description="K9 kıst günü (taslakta)")
    period_days: int | None = None
    lines: list[LineOut]


async def _draft_charges(
    ctx: SiteContext, run: ChargeRun, paging: PageParams
) -> Page[PostedChargeOut]:
    row = await charge_drafts.get_snapshot(ctx.session, run)
    preview, _ = snapshot.load(row.snapshot) if row else (None, [])
    charges = preview.charges if preview else []
    names = ctx.access.can(Permission.PEOPLE_READ)
    page = charges[paging.offset : paging.offset + paging.page_size]
    return Page(
        items=[
            PostedChargeOut(
                id=None,
                unit_id=c.unit_id,
                unit_name=c.unit_name,
                ledger_account_id=c.ledger_account_id,
                reference_code=c.reference_code,
                person_name=c.person_name if names else None,
                account_kind=c.account_kind,
                amount=c.amount,
                days=c.days,
                period_days=c.period_days,
                lines=[LineOut.of(line) for line in c.lines],
            )
            for c in page
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=len(charges),
    )


@router.get("/{run_id}/charges", summary="Koşunun borçları ve kalem dökümü (sayfalı)")
async def run_charges(run_id: uuid.UUID, ctx: FinanceRead, paging: Paging) -> Page[PostedChargeOut]:
    run = await _run(ctx, run_id)
    if run.status not in _POSTED_STATES:
        return await _draft_charges(ctx, run, paging)
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
    names = ctx.access.can(Permission.PEOPLE_READ)  # Denetçi kişisel veri görmez (docs/05)
    current = await charge_adjustments.current_line_amounts(
        session, [line.id for group in lines.values() for line in group]
    )

    def adjusted_total(charge: Charge) -> Decimal | None:
        group = lines[charge.id]
        if not any(line.id in current for line in group):
            return None
        return sum((current.get(line.id, line.amount) for line in group), ZERO)

    items = [
        PostedChargeOut(
            id=charge.id,
            unit_id=charge.unit_id,
            unit_name=f"{block}-{number}" if block else number,
            ledger_account_id=account.id,
            reference_code=account.reference_code,
            person_name=f"{first} {last}" if names else None,
            account_kind=AccountKind(account.kind),
            amount=charge.amount,
            current_amount=adjusted_total(charge),
            lines=[LineOut.of(line, current.get(line.id)) for line in lines[charge.id]],
        )
        for charge, number, block, account, first, last in rows
    ]
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


class PostIn(BaseModel):
    charge_date: date | None = Field(default=None, description="boşsa sıradaki dönemin 1'i")
    due_date: date | None = Field(
        default=None, description="boşsa dönemin vade günü (finans parametresi `due_day`)"
    )


def _submitted_message(result: charge_drafts.Submitted, period: str) -> tuple[int, str]:
    if result.posted is not None:
        posted = result.posted
        return status.HTTP_201_CREATED, (
            f"{posted.period.name} tahakkuku kesildi: {posted.unit_count} bölüm, "
            f"{format_money_tr(posted.total_amount)}."
        )
    number = result.request.number if result.request else None
    return status.HTTP_202_ACCEPTED, (
        f"{period} tahakkuk taslağı 'Tahakkuk' onayına gönderildi (talep #{number}); onaylanınca "
        "tahakkuk tarihinde kesinleşir."
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Tahakkuku kaydet (onay kuralı varsa taslak onaya gider: 202)",
    response_model=Written[ChargeRunOut],
    responses={
        202: {"description": "Taslak onaya gönderildi"},
        409: {"description": "Dönem zaten kesilmiş / kaynak yok / açık taslak var"},
    },
)
async def post_run(
    ctx: ChargePost,
    body: PostIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[ChargeRunOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        run, prepared = await charge_drafts.create(
            ctx.session,
            today,
            created_by=current.user.full_name,
            charge_date=body.charge_date,
            due_date=body.due_date,
        )
        result = await charge_drafts.submit(
            ctx.session, ctx.site, run, actor=actor(current), today=today, now=now
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    [out] = await _runs_out(ctx, [run])
    code, message = _submitted_message(result, prepared.period.name)
    written = Written(data=out, message=message)
    response.status_code = code
    await commit_with_key(ctx.session, idem, code, written)
    return written


@router.post(
    "/drafts",
    status_code=status.HTTP_201_CREATED,
    summary="Tahakkuk taslağı hazırla (deftere yazılmaz)",
    responses={409: {"description": "Dönem kesilmiş / açık taslak var / kaynak yok"}},
)
async def create_draft(
    ctx: ChargePost, body: PostIn, current: CurrentUserDep, today: TodayDep
) -> Written[ChargeRunOut]:
    try:
        run, prepared = await charge_drafts.create(
            ctx.session,
            today,
            created_by=current.user.full_name,
            charge_date=body.charge_date,
            due_date=body.due_date,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _runs_out(ctx, [run])
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"{prepared.period.name} tahakkuk taslağı hazırlandı: {out.charge_count} borç, "
        f"{format_money_tr(out.total_amount)}. İnceleyip onaya gönderin.",
    )


async def _draft_run(ctx: SiteContext, run_id: uuid.UUID) -> ChargeRun:
    run = await _run(ctx, run_id)
    if run.status in _POSTED_STATES:
        raise ApiError(
            HTTPStatus.CONFLICT, "not_a_draft", "Bu koşu kesinleşmiş; taslak işlemi yapılamaz."
        )
    return run


@router.post("/{run_id}/refresh", summary="Taslağı güncel veriyle yeniden hesapla")
async def refresh_draft(
    run_id: uuid.UUID, ctx: ChargePost, today: TodayDep
) -> Written[ChargeRunOut]:
    run = await _draft_run(ctx, run_id)
    try:
        prepared = await charge_drafts.refresh(ctx.session, run, today)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _runs_out(ctx, [run])
    await ctx.session.commit()
    return Written(data=out, message=f"{prepared.period.name} taslağı yeniden hesaplandı.")


@router.post(
    "/{run_id}/submit",
    summary="Taslağı onaya gönder (kural yoksa hemen kesinleşir: 201)",
    response_model=Written[ChargeRunOut],
    responses={202: {"description": "Onaya gönderildi"}},
)
async def submit_draft(
    run_id: uuid.UUID,
    ctx: ChargePost,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[ChargeRunOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        run = await _draft_run(ctx, run_id)
        period = await svc.run_period(ctx.session, run)
        result = await charge_drafts.submit(
            ctx.session, ctx.site, run, actor=actor(current), today=today, now=now
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    [out] = await _runs_out(ctx, [run])
    code, message = _submitted_message(result, period.name)
    written = Written(data=out, message=message)
    response.status_code = code
    await commit_with_key(ctx.session, idem, code, written)
    return written


@router.post("/{run_id}/cancel", summary="Taslaktan vazgeç")
async def cancel_draft(run_id: uuid.UUID, ctx: ChargePost) -> Written[ChargeRunOut]:
    run = await _draft_run(ctx, run_id)
    try:
        await charge_drafts.cancel(ctx.session, run)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _runs_out(ctx, [run])
    await ctx.session.commit()
    return Written(data=out, message=f"{out.period} tahakkuk taslağı iptal edildi.")


@router.get("/{run_id}/diff", summary="Taslağın fark raporu (son kesinleşmiş döneme göre)")
async def draft_diff(run_id: uuid.UUID, ctx: FinanceRead) -> ChargeDiffOut:
    run = await _draft_run(ctx, run_id)
    try:
        prepared = await charge_drafts.prepared_of(ctx.session, run)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    return _diff_out(await charge_diff.report(ctx.session, prepared))


class DraftSummaryOut(BaseModel):
    warnings: list[WarningOut]
    item_totals: list[ItemTotalOut]
    not_due_items: list[str]


@router.get("/{run_id}/summary", summary="Taslağın uyarıları ve kalem toplamları")
async def draft_summary(run_id: uuid.UUID, ctx: FinanceRead) -> DraftSummaryOut:
    run = await _run(ctx, run_id)
    row = await charge_drafts.get_snapshot(ctx.session, run)
    if row is None:
        return DraftSummaryOut(warnings=[], item_totals=[], not_due_items=[])
    preview, not_due = snapshot.load(row.snapshot)
    return DraftSummaryOut(
        warnings=[
            WarningOut(
                kind=w.kind, message=w.message, unit_id=w.unit_id, budget_item_id=w.budget_item_id
            )
            for w in preview.warnings
        ],
        item_totals=[
            ItemTotalOut(
                budget_item_id=t.budget_item_id,
                name=t.name,
                amount=t.amount,
                distributed=t.distributed,
                exempted=t.exempted,
            )
            for t in preview.item_totals
        ],
        not_due_items=not_due,
    )


# --- Satır düzeltmesi (D-10) --------------------------------------------------------------


class ChargeAdjustmentOut(BaseModel):
    id: uuid.UUID
    charge_id: uuid.UUID
    charge_line_id: uuid.UUID
    ledger_account_id: uuid.UUID
    old_amount: Money
    new_amount: Money
    reason: str
    status: Literal["approval_pending", "applied", "rejected"]
    prior_period: bool = Field(description="kilitli döneme ait; açık döneme yazıldı")
    applied_on: date | None
    approval_request_id: uuid.UUID | None
    requested_by: str | None
    created_at: datetime

    @classmethod
    def of(cls, a: ChargeAdjustment) -> ChargeAdjustmentOut:
        return cls(
            id=a.id,
            charge_id=a.charge_id,
            charge_line_id=a.charge_line_id,
            ledger_account_id=a.ledger_account_id,
            old_amount=a.old_amount,
            new_amount=a.new_amount,
            reason=a.reason,
            status=a.status,  # type: ignore[arg-type]
            prior_period=a.prior_period,
            applied_on=a.applied_on,
            approval_request_id=a.approval_request_id,
            requested_by=a.requested_by,
            created_at=a.created_at,
        )


class AdjustmentIn(BaseModel):
    new_amount: Money = Field(description="satırın doğru tutarı (0 olabilir)")
    reason: str = Field(max_length=500, description="gerekçe zorunlu")


@router.post(
    "/lines/{line_id}/adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Kesinleşmiş tahakkukta satır düzeltmesi (onay kuralı varsa 202)",
    response_model=Written[ChargeAdjustmentOut],
    responses={202: {"description": "Onaya gönderildi"}},
)
async def adjust_line(
    line_id: uuid.UUID,
    body: AdjustmentIn,
    ctx: ChargePost,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[ChargeAdjustmentOut] | JSONResponse:
    """Ters kayıt + düzeltilmiş tutarla yeni borç; kilitli dönemde "önceki döneme ait" (FIN-05)."""
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    line = await charge_adjustments.get_line(ctx.session, line_id)
    if line is None:
        raise NotFoundError("Tahakkuk satırı bulunamadı.")
    try:
        result = await charge_adjustments.request(
            ctx.session,
            ctx.site,
            line,
            new_amount=body.new_amount,
            reason=body.reason,
            actor=actor(current),
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    adj = result.adjustment
    change = f"'{line.description}' {format_money_tr(adj.old_amount)} → "
    change += format_money_tr(adj.new_amount)
    if result.request is not None:
        code = status.HTTP_202_ACCEPTED
        message = (
            f"{change} düzeltmesi 'Tahakkuk düzeltme' onayına gönderildi (talep "
            f"#{result.request.number})."
        )
    else:
        code = status.HTTP_201_CREATED
        message = f"{change} düzeltildi: borç ters kaydedildi ve doğru tutarla yeniden yazıldı."
        if adj.prior_period:
            message += " Dönem kilitli olduğu için açık döneme 'önceki döneme ait' yazıldı."
    written = Written(data=ChargeAdjustmentOut.of(adj), message=message)
    response.status_code = code
    await commit_with_key(ctx.session, idem, code, written)
    return written


@router.get("/{run_id}/adjustments", summary="Koşunun satır düzeltmeleri")
async def run_adjustments(run_id: uuid.UUID, ctx: FinanceRead) -> list[ChargeAdjustmentOut]:
    run = await _run(ctx, run_id)
    charge_ids = list(
        await ctx.session.scalars(select(Charge.id).where(Charge.charge_run_id == run.id))
    )
    return [
        ChargeAdjustmentOut.of(a)
        for a in await charge_adjustments.list_for_charges(ctx.session, charge_ids)
    ]


class ReverseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="ters kayıt gerekçesi")


@router.post(
    "/{run_id}/reverse",
    summary="Koşuyu ters kayıtla iptal et",
    response_model=Written[ChargeRunOut] | Written[ApprovalPendingOut],
    responses={
        202: {"description": "`status: approval_pending` — iptal onaya gönderildi"},
        409: {"description": "Zaten ters kaydedilmiş / ters kaydedilemez"},
    },
)
async def reverse_run(
    run_id: uuid.UUID,
    ctx: ChargePost,
    body: ReverseIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[ChargeRunOut] | Written[ApprovalPendingOut] | JSONResponse:
    """Onay kuralı (`charge_run_reversal`) tanımlıysa 202 `approval_pending`."""
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        run = await _run(ctx, run_id)
        [current_run] = await _runs_out(ctx, [run])
        request = await charge_approvals.request_reversal_if_ruled(
            ctx.session,
            ctx.site,
            run,
            reason=" ".join(body.reason.split()),
            total=current_run.total_amount,
            requester=Requester(current.user.id, current.user.full_name),
            today=today,
            now=now,
        )
        if request is not None:
            pending = Written(
                data=ApprovalPendingOut(approval_request_id=request.id),
                message=f"{current_run.period} tahakkuk iptali onaya gönderildi (talep "
                f"#{request.number}); onaylanınca ters kayıt yazılır.",
            )
            response.status_code = status.HTTP_202_ACCEPTED
            await commit_with_key(ctx.session, idem, status.HTTP_202_ACCEPTED, pending)
            return pending
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
    except ApprovalError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    [out] = await _runs_out(ctx, [reversal])
    result = Written(
        data=out,
        message=f"{out.period} tahakkuku ters kayıtla iptal edildi; dönem yeniden kesilebilir.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result
