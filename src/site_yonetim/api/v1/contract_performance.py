"""Sözleşme performansı — servis istekleri 68–70 (Yazılım Dokümanı 15.3, SAT-06).

- 68: `GET|PUT …/contracts/{id}/performance` — tedarikçi bağı, KPI, ceza ve artış kuralı.
- 69: `GET|PATCH …/contracts/{id}/scorecards/{YYYY-MM}`, `POST …/finalize`, `POST …/penalty`;
  `GET …/contracts/{id}/scorecards` (sayfalı karne listesi).
- 70: `POST …/contracts/{id}/escalation` (artış kararı); önizleme performans yanıtında.
  Endeks değerleri `/platform/index-values` (ayrı yönlendirici, yalnız platform yöneticisi).

Okuma `expenses.read` (sözleşme okuma), yazma `contracts.manage`. Ceza kesintisi kararı: "Ceza
kesintisi" onay kuralı varsa onay mekanizmasındaki onaycılar (izin onay kuralından gelir), yoksa
`contracts.manage`; karneyi kesinleştiren onaylayamaz (403 `self_approval`). Artış onayı "Sözleşme
artışı" onay kuralına tabi (202 `approval_pending`).
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import finance_error
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ApprovalStatus
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.contract_performance import (
    CutKind,
    EscalationInput,
    EscalationKind,
    EscalationPeriod,
    EscalationStatus,
    KpiInput,
    KpiSource,
    PenaltyInput,
    PenaltyStatus,
    PreviewStatus,
    ScorecardStatus,
    monthly_amount,
)
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.text import format_date_tr, format_money_tr
from site_yonetim.models import Contract, ContractEscalation, ContractScorecard
from site_yonetim.services import contract_performance as svc
from site_yonetim.services import contracts as contracts_svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/contracts/{contract_id}", tags=["sözleşme"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.CONTRACTS_MANAGE))]


def perf_error(exc: svc.PerformanceError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


async def _contract(ctx: SiteContext, contract_id: uuid.UUID, *, lock: bool = False) -> Contract:
    item = (
        await contracts_svc.get(ctx.session, contract_id)
        if lock
        else await ctx.session.get(Contract, contract_id)
    )
    if item is None:
        raise NotFoundError("Sözleşme bulunamadı.")
    return item


# --- 68 Performans tanımları --------------------------------------------------------------


class ContractKpiOut(BaseModel):
    id: uuid.UUID
    name: str
    source: KpiSource
    target: Decimal = Field(description="hedef %, 1–100")
    weight: int
    request_category: str | None


class ContractPenaltyRuleOut(BaseModel):
    id: uuid.UUID
    kpi_id: uuid.UUID
    below_percent: Decimal = Field(description="hedefin bu yüzdesinden fazla altındaysa (0–99)")
    cut_kind: CutKind
    cut_value: Money = Field(description="percent: oran (1–100) · fixed: TL")


class ContractEscalationRuleOut(BaseModel):
    kind: EscalationKind
    fixed_rate: Decimal | None
    period: EscalationPeriod
    cap: Decimal | None


class ContractEscalationRecordOut(BaseModel):
    id: uuid.UUID
    date: dt.date = Field(description="artış tarihi (yeni bedel bu günden geçerli)")
    kind: EscalationKind
    index_period: str | None
    rate: Decimal
    capped_rate: Decimal
    old_amount: Money
    new_amount: Money
    status: EscalationStatus
    note: str | None
    approval_request_id: uuid.UUID | None
    requested_by: str
    by: str | None = Field(description="karar veren(ler)")
    at: dt.datetime | None

    @classmethod
    def of(cls, r: ContractEscalation) -> ContractEscalationRecordOut:
        return cls(
            id=r.id,
            date=r.due_date,
            kind=EscalationKind(r.kind),
            index_period=r.index_period,
            rate=r.rate,
            capped_rate=r.capped_rate,
            old_amount=r.old_amount,
            new_amount=r.new_amount,
            status=EscalationStatus(r.status),
            note=r.note,
            approval_request_id=r.approval_request_id,
            requested_by=r.requested_by,
            by=r.decided_by,
            at=r.decided_at,
        )


class EscalationPreviewOut(BaseModel):
    status: PreviewStatus
    date: dt.date | None
    days_left: int | None
    index_period: str | None
    rate: Decimal | None
    capped_rate: Decimal | None
    old_amount: Money | None
    new_amount: Money | None
    annual_effect: Money | None = Field(description="(yeni − eski) × yıllık dönem sayısı")
    record: ContractEscalationRecordOut | None

    @classmethod
    def of(cls, p: svc.Preview) -> EscalationPreviewOut:
        return cls(
            status=p.status,
            date=p.date,
            days_left=p.days_left,
            index_period=p.index_period,
            rate=p.rate,
            capped_rate=p.capped_rate,
            old_amount=p.old_amount,
            new_amount=p.new_amount,
            annual_effect=p.annual_effect,
            record=ContractEscalationRecordOut.of(p.record) if p.record else None,
        )


class ScorecardSummaryOut(BaseModel):
    period: str
    total: int | None
    status: ScorecardStatus
    penalty: Money | None = Field(description="onaylanan kesinti")
    penalty_status: PenaltyStatus | None


class ContractVendorOut(BaseModel):
    id: uuid.UUID
    name: str
    rating_avg: Decimal | None
    rating_count: int


class ContractPerformanceOut(BaseModel):
    vendor_id: uuid.UUID | None = Field(
        description="tedarikçi kartı (P10); kart bilgisi satın alma paketiyle gelecek"
    )
    vendor: ContractVendorOut | None
    kpis: list[ContractKpiOut]
    penalties: list[ContractPenaltyRuleOut]
    escalation: ContractEscalationRuleOut
    escalations: list[ContractEscalationRecordOut] = Field(description="artış geçmişi")
    monthly_amount: Money | None = Field(description="bugün geçerli aylık bedel")
    escalation_preview: EscalationPreviewOut
    last_scorecards: list[ScorecardSummaryOut] = Field(description="son 12 karne, yeniden eskiye")


async def _performance(
    ctx: SiteContext, contract: Contract, today: dt.date
) -> ContractPerformanceOut:
    loaded = await svc.load(ctx.session, contract.id)
    rule = svc.escalation_of(contract)
    rows = await contracts_svc.amount_rows(ctx.session, contract.id)
    amount, period = (
        contracts_svc.amount_at(rows, today) if rows else (contract.amount, contract.period)
    )
    cards = await svc.last_cards(ctx.session, contract.id)
    return ContractPerformanceOut(
        vendor_id=contract.vendor_id,
        vendor=None,
        kpis=[
            ContractKpiOut(
                id=k.id,
                name=k.name,
                source=KpiSource(k.source),
                target=k.target,
                weight=k.weight,
                request_category=k.request_category,
            )
            for k in loaded.kpis
        ],
        penalties=[
            ContractPenaltyRuleOut(
                id=p.id,
                kpi_id=p.kpi_id,
                below_percent=p.below_percent,
                cut_kind=CutKind(p.cut_kind),
                cut_value=p.cut_value,
            )
            for p in loaded.penalties
        ],
        escalation=ContractEscalationRuleOut(
            kind=rule.kind, fixed_rate=rule.fixed_rate, period=rule.period, cap=rule.cap
        ),
        escalations=[
            ContractEscalationRecordOut.of(r)
            for r in await svc.escalation_history(ctx.session, contract.id)
        ],
        monthly_amount=monthly_amount(amount, period),
        escalation_preview=EscalationPreviewOut.of(
            await svc.preview(ctx.session, contract, today=today)
        ),
        last_scorecards=[
            ScorecardSummaryOut(
                period=c.period,
                total=c.total if c.status == ScorecardStatus.FINALIZED.value else None,
                status=ScorecardStatus(c.status),
                penalty=p.amount if p and p.status == PenaltyStatus.APPROVED.value else None,
                penalty_status=PenaltyStatus(p.status) if p else None,
            )
            for c, p in cards
        ],
    )


@router.get("/performance", summary="Sözleşme performansı: KPI, ceza, artış kuralı ve önizleme")
async def get_performance(
    contract_id: uuid.UUID, ctx: Reader, today: TodayDep
) -> ContractPerformanceOut:
    return await _performance(ctx, await _contract(ctx, contract_id), today)


class ContractKpiIn(BaseModel):
    id: str | None = Field(
        default=None, max_length=64, description="mevcut KPI ya da geçici kimlik"
    )
    name: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=30)
    target: Decimal | None = None
    weight: int | None = None
    request_category: str | None = Field(default=None, max_length=60)


class ContractPenaltyRuleIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    kpi_id: str | None = Field(default=None, max_length=64)
    below_percent: Decimal | None = None
    cut_kind: str | None = Field(default=None, max_length=20)
    cut_value: Money | None = None


class ContractEscalationRuleIn(BaseModel):
    kind: str | None = Field(default=None, max_length=20)
    fixed_rate: Decimal | None = None
    period: str | None = Field(default=None, max_length=20)
    cap: Decimal | None = None


class ContractPerformanceIn(BaseModel):
    vendor_id: uuid.UUID | None = None
    kpis: list[ContractKpiIn] | None = Field(default=None, max_length=50)
    penalties: list[ContractPenaltyRuleIn] | None = Field(default=None, max_length=50)
    escalation: ContractEscalationRuleIn | None = None


@router.put("/performance", summary="Performans kurallarını kaydet (KPI, ceza, artış)")
async def put_performance(
    contract_id: uuid.UUID, body: ContractPerformanceIn, ctx: Manager, today: TodayDep
) -> Written[ContractPerformanceOut]:
    """422 `fields.kpis.{i}.*`, `fields.kpis` (ağırlık toplamı), `fields.penalties.{i}.*`,
    `fields.escalation.*`."""
    contract = await _contract(ctx, contract_id, lock=True)
    data = svc.DefinitionsInput(
        vendor_id=body.vendor_id,
        vendor_given="vendor_id" in body.model_fields_set,
        kpis=None
        if body.kpis is None
        else [
            KpiInput(k.id or f"new-{i}", k.name, k.source, k.target, k.weight, k.request_category)
            for i, k in enumerate(body.kpis)
        ],
        penalties=None
        if body.penalties is None
        else [
            PenaltyInput(p.kpi_id, p.below_percent, p.cut_kind, p.cut_value) for p in body.penalties
        ],
        escalation=None
        if body.escalation is None
        else EscalationInput(
            body.escalation.kind,
            body.escalation.fixed_rate,
            body.escalation.period,
            body.escalation.cap,
        ),
    )
    try:
        await svc.save_definitions(ctx.session, contract, data, today=today)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = await _performance(ctx, contract, today)
    await ctx.session.commit()
    return Written(data=out, message="Sözleşme performans kuralları kaydedildi.")


# --- 69 Aylık karne ------------------------------------------------------------------------


class ScorecardRowOut(BaseModel):
    kpi_id: str
    name: str
    source: KpiSource
    target: Decimal
    weight: int
    actual: Decimal | None
    score: int | None
    detail: str | None = Field(description="kaynak açıklaması, ör. '3 denetim ortalaması'")
    editable: bool = Field(description="elle girilir (elle KPI, kaynak kapalı ya da veri yok)")


class ScorecardPenaltyOut(BaseModel):
    amount: Money
    reasons: list[str]
    status: Literal["none", "pending", "approved", "rejected"] = Field(
        description="none: taslakta hesaplandı, kesinleşince onaya düşer"
    )
    decided_by: str | None
    note: str | None
    approval_request_id: uuid.UUID | None
    notice: str | None = Field(description="onaydan sonra tedarikçi bildirim taslağı")


class ScorecardOut(BaseModel):
    id: uuid.UUID | None = Field(description="henüz kaydedilmemiş taslakta boş")
    contract_id: uuid.UUID
    period: str
    rows: list[ScorecardRowOut]
    total: int | None
    comment: str | None
    status: ScorecardStatus
    penalty: ScorecardPenaltyOut | None
    monthly_amount: Money | None = Field(description="dönemin ilk günü geçerli aylık bedel")
    expected_invoice: Money | None = Field(description="aylık bedel − onaylanan kesinti")
    finalized_by: str | None
    finalized_by_id: uuid.UUID | None
    finalized_at: dt.datetime | None

    @classmethod
    def of(cls, v: svc.CardView) -> ScorecardOut:
        penalty: ScorecardPenaltyOut | None = None
        if v.penalty is not None:
            penalty = ScorecardPenaltyOut(
                amount=v.penalty.amount,
                reasons=list(v.penalty.reasons),
                status=v.penalty.status,  # type: ignore[arg-type]
                decided_by=v.penalty.decided_by,
                note=v.penalty.note,
                approval_request_id=v.penalty.approval_request_id,
                notice=svc.supplier_notice(v.contract, v),
            )
        elif v.computed_penalty is not None:
            penalty = ScorecardPenaltyOut(
                amount=v.computed_penalty.amount,
                reasons=v.computed_penalty.reasons,
                status="none",
                decided_by=None,
                note=None,
                approval_request_id=None,
                notice=None,
            )
        card = v.card
        return cls(
            id=card.id if card else None,
            contract_id=v.contract.id,
            period=v.period,
            rows=[
                ScorecardRowOut(
                    kpi_id=r.kpi_id,
                    name=r.name,
                    source=r.source,
                    target=r.target,
                    weight=r.weight,
                    actual=r.actual,
                    score=r.score,
                    detail=r.detail,
                    editable=r.editable,
                )
                for r in v.rows
            ],
            total=v.total,
            comment=v.comment,
            status=v.status,
            penalty=penalty,
            monthly_amount=v.monthly_amount,
            expected_invoice=v.expected_invoice,
            finalized_by=card.finalized_by if card else None,
            finalized_by_id=card.finalized_by_id if card else None,
            finalized_at=card.finalized_at if card else None,
        )


def _period(contract: Contract, value: str, today: dt.date) -> YearMonth:
    try:
        period = svc.parse_period(value)
        svc.check_period(contract, period, today)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    return period


@router.get("/scorecards", summary="Karneler (yeniden eskiye, sayfalı)")
async def list_scorecards(
    contract_id: uuid.UUID, ctx: Reader, params: Annotated[PageParams, Depends()]
) -> Page[ScorecardSummaryOut]:
    contract = await _contract(ctx, contract_id)
    where = ContractScorecard.contract_id == contract.id
    total = await ctx.session.scalar(select(func.count()).where(where)) or 0
    cards = list(
        await ctx.session.scalars(
            select(ContractScorecard)
            .where(where)
            .order_by(ContractScorecard.period.desc())
            .offset(params.offset)
            .limit(params.page_size)
        )
    )
    items = []
    for c in cards:
        p = await svc.penalty_of(ctx.session, c)
        items.append(
            ScorecardSummaryOut(
                period=c.period,
                total=c.total if c.status == ScorecardStatus.FINALIZED.value else None,
                status=ScorecardStatus(c.status),
                penalty=p.amount if p and p.status == PenaltyStatus.APPROVED.value else None,
                penalty_status=PenaltyStatus(p.status) if p else None,
            )
        )
    return Page(items=items, page=params.page, page_size=params.page_size, total=total)


@router.get("/scorecards/{period}", summary="Aylık hizmet karnesi (taslak kaynaklardan hesaplanır)")
async def get_scorecard(
    contract_id: uuid.UUID, period: str, ctx: Reader, today: TodayDep
) -> ScorecardOut:
    contract = await _contract(ctx, contract_id)
    ym = _period(contract, period, today)
    return ScorecardOut.of(
        await svc.view(ctx.session, contract, ym, today=today, modules=ctx.modules)
    )


class ScorecardPatchIn(BaseModel):
    values: dict[str, Decimal | None] | None = Field(
        default=None, description="{kpi_id: 0–100 | null} — yalnız elle girilebilen KPI'lar"
    )
    comment: str | None = Field(default=None, max_length=2000)


@router.patch("/scorecards/{period}", summary="Elle değerleri ve yorumu kaydet (taslak)")
async def patch_scorecard(
    contract_id: uuid.UUID, period: str, body: ScorecardPatchIn, ctx: Manager, today: TodayDep
) -> Written[ScorecardOut]:
    """409 `finalized`; 422 `fields.values.{kpi_id}`."""
    contract = await _contract(ctx, contract_id)
    ym = _period(contract, period, today)
    try:
        out = await svc.save_values(
            ctx.session,
            contract,
            ym,
            values=body.values,
            comment=body.comment,
            comment_given="comment" in body.model_fields_set,
            today=today,
            modules=ctx.modules,
        )
    except svc.PerformanceError as exc:
        raise perf_error(exc) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    data = ScorecardOut.of(out)
    await ctx.session.commit()
    return Written(data=data, message="Karne güncellendi.")


@router.post("/scorecards/{period}/finalize", summary="Karneyi kesinleştir")
async def finalize_scorecard(
    contract_id: uuid.UUID,
    period: str,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ScorecardOut]:
    """409 `finalized`, `period_open`, `no_kpis`; 422 `missing_values`. Ceza tetiklenirse kesinti
    onaya düşer ("Ceza kesintisi" kuralı varsa onay mekanizmasına)."""
    contract = await _contract(ctx, contract_id, lock=True)
    ym = _period(contract, period, today)
    try:
        result = await svc.finalize(
            ctx.session,
            ctx.site,
            contract,
            ym,
            actor=actor(current),
            today=today,
            now=now,
            modules=ctx.modules,
        )
    except svc.PerformanceError as exc:
        raise perf_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    data = ScorecardOut.of(result.view)
    await ctx.session.commit()
    penalty = result.view.penalty
    if penalty is None:
        message = "Karne kesinleşti; ceza kuralı tetiklenmedi."
    elif result.request is not None:
        message = (
            f"Karne kesinleşti; {format_money_tr(penalty.amount)} ceza kesintisi onaya gönderildi "
            f"(talep #{result.request.number})."
        )
    else:
        message = f"Karne kesinleşti; {format_money_tr(penalty.amount)} ceza kesintisi onaya düştü."
    return Written(data=data, message=message)


class PenaltyDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


@router.post("/scorecards/{period}/penalty", summary="Ceza kesintisini onayla / reddet")
async def decide_penalty(
    contract_id: uuid.UUID,
    period: str,
    body: PenaltyDecisionIn,
    ctx: SiteContextDep,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ScorecardOut]:
    """403 `self_approval` (karneyi kesinleştiren), `forbidden`; 404; 409 `invalid_status`,
    `period_locked`; 422 `fields.note` (retde gerekçe)."""
    contract = await _contract(ctx, contract_id, lock=True)
    ym = _period(contract, period, today)
    approve = body.decision == "approve"
    try:
        request = await svc.decide_penalty(
            ctx.session,
            ctx.site,
            contract,
            ym,
            approve=approve,
            note=body.note,
            actor=actor(current),
            can_manage=ctx.access.can(Permission.CONTRACTS_MANAGE),
            now=now,
        )
    except svc.PerformanceError as exc:
        raise perf_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = await svc.view(ctx.session, contract, ym, today=today, modules=ctx.modules)
    data = ScorecardOut.of(out)
    await ctx.session.commit()
    status = out.penalty.status if out.penalty else None
    if status == PenaltyStatus.APPROVED.value:
        message = (
            "Ceza kesintisi onaylandı; dönem faturasının beklenen tutarı düştü, tedarikçiye "
            "bildirim taslağı hazır."
        )
    elif status == PenaltyStatus.REJECTED.value:
        message = "Ceza kesintisi reddedildi."
    elif request is not None and ApprovalStatus(request.status) is ApprovalStatus.PENDING:
        message = f"Kararınız kaydedildi; talep #{request.number} diğer onayları bekliyor."
    else:
        message = "Kararınız kaydedildi."
    return Written(data=data, message=message)


# --- 70 Endeksli artış ---------------------------------------------------------------------


class EscalationDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


@router.post(
    "/escalation",
    summary="Sıradaki artışa karar ver (yeni bedel / bedel değişmesin)",
    responses={202: {"description": "Sözleşme artışı onay kuralına gönderildi"}},
)
async def decide_escalation(
    contract_id: uuid.UUID,
    body: EscalationDecisionIn,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[EscalationPreviewOut]:
    """409 `not_due` (30 günden fazla var / endeks eksik), `period_locked`; 422 `fields.note`."""
    contract = await _contract(ctx, contract_id, lock=True)
    try:
        result = await svc.decide_escalation(
            ctx.session,
            ctx.site,
            contract,
            approve=body.decision == "approve",
            note=body.note,
            actor=actor(current),
            today=today,
            now=now,
        )
    except svc.PerformanceError as exc:
        raise perf_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = EscalationPreviewOut.of(await svc.preview(ctx.session, contract, today=today))
    await ctx.session.commit()
    record = result.record
    day = format_date_tr(record.due_date)
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        message = (
            f"Yeni bedel ({format_money_tr(record.new_amount)}) onaya gönderildi "
            f"(talep #{result.request.number}); onaylanınca {day} tarihinden geçerli olur."
        )
    elif record.status == EscalationStatus.APPROVED.value:
        message = (
            f"Yeni bedel {format_money_tr(record.new_amount)}, {day} tarihinden itibaren geçerli."
        )
    else:
        message = "Bedel değişmedi; görüşme notu kaydedildi."
    return Written(data=out, message=message)
