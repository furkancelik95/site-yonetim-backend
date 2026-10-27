"""Aidat kuralları — Yazılım Dokümanı FIN-02; PM K23 ("ikisi de olsun, site seçsin").

Okuma `finance.read`, yazma `finance.budget.manage`. Kural sürümü taslakken düzenlenir, deneme
hesabı kayıt oluşturmaz; onaya gönderilen sürüm "Aidat kuralı" onay kuralı varsa onay bekler
(202), yoksa doğrudan yürürlüğe girer. Tahakkuk motoru kuralları, finans parametresi
`charge_source = billing_rule` olan sitede kullanır.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import BudgetManage, FinanceRead, finance_error
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.charging.billing_rules import VersionFields
from site_yonetim.domain.charging.engine import ComponentData, Exemption, WarningKind
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    FinanceRuleError,
    Frequency,
    ScopeKind,
)
from site_yonetim.domain.fiscal import parse_period_key, period_key
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import BillingRule, BillingRuleVersion, ChargeType
from site_yonetim.services import billing_rules as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/billing-rules", tags=["aidat kuralları"])

VersionStatus = Literal["draft", "approval_pending", "active", "rejected"]


class RuleComponentIn(BaseModel):
    kind: AllocationKind
    percent: Decimal = Field(description="yüzde (ör. 70)")
    area_basis: AreaBasis = AreaBasis.GROSS


class TypeCoefficientIn(BaseModel):
    unit_type_id: uuid.UUID
    coefficient: Decimal = Field(description="ör. dükkan 1.5 · daire 1.0")


class ExemptionIn(BaseModel):
    unit_id: uuid.UUID
    percent: Decimal = Field(description="indirim oranı (%), 100 = tam muaf")
    reason: str = Field(max_length=100, description="ör. boş daire, personel dairesi")


class BillingVersionIn(BaseModel):
    effective_period: str = Field(description="yürürlük dönemi YYYY-AA; geçmiş/kilitli olamaz")
    kind: AllocationKind = Field(
        description="equal · by_area · by_land_share · by_unit_type_weight · fixed_per_unit · "
        "composite"
    )
    area_basis: AreaBasis = AreaBasis.GROSS
    amount: Money = Field(
        description="bölüm başına sabitte bölüm başına tutar, diğerlerinde dönem toplamı"
    )
    frequency: Frequency = Frequency.MONTHLY
    components: list[RuleComponentIn] = Field(default_factory=list)
    type_coefficients: list[TypeCoefficientIn] = Field(default_factory=list)
    exemptions: list[ExemptionIn] = Field(default_factory=list)
    scope_kind: ScopeKind = ScopeKind.WHOLE_SITE
    scope_block_ids: list[uuid.UUID] = Field(default_factory=list)
    scope_unit_type_ids: list[uuid.UUID] = Field(default_factory=list)
    scope_usage: str | None = None
    discontinued: bool = Field(default=False, description="kural bu dönemden itibaren durur")
    basis_note: str = Field(max_length=300, description="dayanak: yönetim planı md. / kurul kararı")
    basis_document_id: uuid.UUID | None = Field(default=None, description="belge arşivinden")

    def fields(self) -> VersionFields:
        period = parse_period_key(self.effective_period)
        if period is None:
            raise FormFieldError(
                {"effective_period": "Dönem YYYY-AA biçiminde olmalı (ör. 2026-11)."}
            )
        return VersionFields(
            effective=period,
            kind=self.kind,
            amount=self.amount,
            basis_note=self.basis_note,
            area_basis=self.area_basis,
            frequency=self.frequency,
            components=tuple(
                ComponentData(c.kind, c.percent, c.area_basis) for c in self.components
            ),
            type_coefficients={c.unit_type_id: c.coefficient for c in self.type_coefficients},
            exemptions={e.unit_id: Exemption(e.percent, e.reason) for e in self.exemptions},
            scope_kind=self.scope_kind,
            scope_block_ids=frozenset(self.scope_block_ids),
            scope_unit_type_ids=frozenset(self.scope_unit_type_ids),
            scope_usage=self.scope_usage,
            discontinued=self.discontinued,
        )


class BillingRuleIn(BaseModel):
    name: str = Field(max_length=120)
    charge_type_id: uuid.UUID = Field(description="kalem tipi — kimin ödeyeceğini belirler")
    owner_item: bool = Field(default=False, description="malik kalemi: malike, kiracıya görünmez")


class BillingRuleCreateIn(BillingRuleIn):
    version: BillingVersionIn


class BillingVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    status: VersionStatus
    effective_period: str = Field(description="YYYY-MM")
    kind: AllocationKind
    area_basis: AreaBasis
    amount_mode: Literal["per_unit", "total"]
    amount: Money
    frequency: Frequency
    components: list[RuleComponentIn]
    type_coefficients: list[TypeCoefficientIn]
    exemptions: list[ExemptionIn]
    scope_kind: ScopeKind
    scope_block_ids: list[uuid.UUID]
    scope_unit_type_ids: list[uuid.UUID]
    scope_usage: str | None
    discontinued: bool
    basis_note: str
    basis_document_id: uuid.UUID | None
    approval_request_id: uuid.UUID | None
    created_by: str | None
    created_at: dt.datetime
    activated_at: dt.datetime | None

    @classmethod
    def of(cls, v: BillingRuleVersion) -> BillingVersionOut:
        f = svc.fields_of(v)
        return cls(
            id=v.id,
            version_no=v.version_no,
            status=v.status,  # type: ignore[arg-type]
            effective_period=period_key(f.effective),
            kind=f.kind,
            area_basis=f.area_basis,
            amount_mode=v.amount_mode,  # type: ignore[arg-type]
            amount=v.amount,
            frequency=f.frequency,
            components=[
                RuleComponentIn(kind=c.kind, percent=c.percent, area_basis=c.area_basis)
                for c in f.components
            ],
            type_coefficients=[
                TypeCoefficientIn(unit_type_id=k, coefficient=c)
                for k, c in f.type_coefficients.items()
            ],
            exemptions=[
                ExemptionIn(unit_id=k, percent=e.percent, reason=e.reason)
                for k, e in f.exemptions.items()
            ],
            scope_kind=f.scope_kind,
            scope_block_ids=sorted(f.scope_block_ids),
            scope_unit_type_ids=sorted(f.scope_unit_type_ids),
            scope_usage=f.scope_usage,
            discontinued=v.discontinued,
            basis_note=v.basis_note,
            basis_document_id=v.basis_document_id,
            approval_request_id=v.approval_request_id,
            created_by=v.created_by,
            created_at=v.created_at,
            activated_at=v.activated_at,
        )


class BillingRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    charge_type_id: uuid.UUID
    charge_type_name: str
    owner_item: bool
    current_version: BillingVersionOut | None = Field(description="bu dönemde geçerli sürüm")
    versions: list[BillingVersionOut] = Field(description="yeniden eskiye")


async def _out(ctx: SiteContext, rules: list[BillingRule], today: dt.date) -> list[BillingRuleOut]:
    versions = await svc.versions_of(ctx.session, [r.id for r in rules])
    types = {t.id: t.name for t in await ctx.session.scalars(select(ChargeType))}
    period = YearMonth.of(today)
    out = []
    for rule in rules:
        current = svc.effective_version(versions[rule.id], period)
        out.append(
            BillingRuleOut(
                id=rule.id,
                name=rule.name,
                charge_type_id=rule.charge_type_id,
                charge_type_name=types.get(rule.charge_type_id, "—"),
                owner_item=rule.owner_item,
                current_version=BillingVersionOut.of(current) if current else None,
                versions=[BillingVersionOut.of(v) for v in versions[rule.id]],
            )
        )
    return out


async def _rule(ctx: SiteContext, rule_id: uuid.UUID) -> BillingRule:
    rule = await svc.get_rule(ctx.session, rule_id)
    if rule is None:
        raise NotFoundError("Aidat kuralı bulunamadı.")
    return rule


async def _version(
    ctx: SiteContext, rule: BillingRule, version_id: uuid.UUID
) -> BillingRuleVersion:
    version = await ctx.session.get(BillingRuleVersion, version_id)
    if version is None or version.rule_id != rule.id:
        raise NotFoundError("Kural sürümü bulunamadı.")
    return version


@router.get("", summary="Aidat kuralları (geçerli sürüm ve sürüm geçmişi)")
async def list_rules(ctx: FinanceRead, today: TodayDep) -> list[BillingRuleOut]:
    return await _out(ctx, await svc.list_rules(ctx.session), today)


@router.get("/{rule_id}", summary="Aidat kuralı")
async def get_rule(rule_id: uuid.UUID, ctx: FinanceRead, today: TodayDep) -> BillingRuleOut:
    [out] = await _out(ctx, [await _rule(ctx, rule_id)], today)
    return out


@router.post("", status_code=HTTPStatus.CREATED, summary="Aidat kuralı ve ilk sürümü (taslak)")
async def create_rule(
    body: BillingRuleCreateIn, ctx: BudgetManage, current: CurrentUserDep, today: TodayDep
) -> Written[BillingRuleOut]:
    try:
        rule, _ = await svc.create(
            ctx.session,
            svc.RuleInput(body.name, body.charge_type_id, body.owner_item),
            body.version.fields(),
            document_id=body.version.basis_document_id,
            actor=actor(current),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    [out] = await _out(ctx, [rule], today)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"'{rule.name}' taslak olarak kaydedildi; deneme hesabından sonra onaya gönderin.",
    )


@router.patch("/{rule_id}", summary="Kuralın adı, kalem tipi, malik kalemi")
async def update_rule(
    rule_id: uuid.UUID, body: BillingRuleIn, ctx: BudgetManage, today: TodayDep
) -> Written[BillingRuleOut]:
    rule = await _rule(ctx, rule_id)
    try:
        await svc.update_rule(
            ctx.session, rule, svc.RuleInput(body.name, body.charge_type_id, body.owner_item)
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _out(ctx, [rule], today)
    await ctx.session.commit()
    return Written(data=out, message="Kural güncellendi.")


@router.post("/{rule_id}/versions", status_code=HTTPStatus.CREATED, summary="Yeni sürüm (taslak)")
async def add_version(
    rule_id: uuid.UUID,
    body: BillingVersionIn,
    ctx: BudgetManage,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[BillingRuleOut]:
    rule = await _rule(ctx, rule_id)
    try:
        version = await svc.add_version(
            ctx.session,
            rule,
            body.fields(),
            document_id=body.basis_document_id,
            actor=actor(current),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _out(ctx, [rule], today)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"Sürüm {version.version_no} taslak olarak kaydedildi; yalnız yürürlüğünden "
        "sonraki dönemleri etkiler.",
    )


@router.put("/{rule_id}/versions/{version_id}", summary="Taslak sürümü düzenle")
async def update_version(
    rule_id: uuid.UUID,
    version_id: uuid.UUID,
    body: BillingVersionIn,
    ctx: BudgetManage,
    today: TodayDep,
) -> Written[BillingRuleOut]:
    rule = await _rule(ctx, rule_id)
    version = await _version(ctx, rule, version_id)
    try:
        await svc.update_version(
            ctx.session, version, body.fields(), document_id=body.basis_document_id
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    [out] = await _out(ctx, [rule], today)
    await ctx.session.commit()
    return Written(data=out, message=f"Sürüm {version.version_no} güncellendi.")


@router.post(
    "/{rule_id}/versions/{version_id}/submit",
    summary="Sürümü onaya gönder (kural yoksa hemen yürürlüğe girer)",
    responses={202: {"description": "Onaya gönderildi"}},
)
async def submit_version(
    rule_id: uuid.UUID,
    version_id: uuid.UUID,
    ctx: BudgetManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[BillingRuleOut]:
    """409 `missing_unit_data` (eksik bölüm verisi), `scope_overlap`, `period_locked`."""
    rule = await _rule(ctx, rule_id)
    version = await _version(ctx, rule, version_id)
    try:
        result = await svc.submit(
            ctx.session, ctx.site, rule, version, actor=actor(current), today=today, now=now
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    [out] = await _out(ctx, [rule], today)
    await ctx.session.commit()
    period = f"{version.effective_month:02d}/{version.effective_year}"
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        message = (
            f"'{rule.name}' sürüm {version.version_no} 'Aidat kuralı' onayına gönderildi (talep "
            f"#{result.request.number}); onaylanınca {period} döneminden yürürlüğe girer."
        )
    else:
        message = f"'{rule.name}' sürüm {version.version_no} {period} döneminden yürürlükte."
    return Written(data=out, message=message)


# --- Deneme hesabı --------------------------------------------------------------------------


class TrialLineOut(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    amount: Money
    explanation: str


class TrialWarningOut(BaseModel):
    kind: WarningKind
    message: str


class BillingTrialOut(BaseModel):
    lines: list[TrialLineOut] = Field(description="bölüm bazında tutar (ödeyenden bağımsız)")
    unit_count: int
    period_total: Money = Field(description="bir dönemde yazılacak toplam")
    pool: Money = Field(description="kuralın dönem tutarı (sabitte bölüm × tutar)")
    exempted: Money = Field(description="muafiyetle yazılmayan")
    annual_total: Money = Field(description="yıllık karşılığı (dönem × yıldaki dönem sayısı)")
    budget_income_target: Money | None = Field(
        description="yürürlükteki yıllık bütçenin gelir hedefi (FIN-17); yoksa null"
    )
    warnings: list[TrialWarningOut]


class BillingTrialIn(BillingRuleIn):
    version: BillingVersionIn


def _trial_out(result: svc.TrialResult) -> BillingTrialOut:
    sim = result.simulation
    return BillingTrialOut(
        lines=[
            TrialLineOut(
                unit_id=unit.id,
                unit_name=unit.name,
                amount=line.amount,
                explanation=line.explanation,
            )
            for unit, line in sim.lines
        ],
        unit_count=len(sim.lines),
        period_total=result.period_total,
        pool=sim.total.amount,
        exempted=sim.total.exempted,
        annual_total=result.annual_total,
        budget_income_target=result.budget_income_target,
        warnings=[TrialWarningOut(kind=w.kind, message=w.message) for w in sim.warnings],
    )


@router.post("/trial", summary="Deneme hesabı (kayıt oluşturmaz)")
async def trial(body: BillingTrialIn, ctx: FinanceRead) -> BillingTrialOut:
    try:
        result = await svc.trial(
            ctx.session,
            svc.RuleInput(body.name, body.charge_type_id, body.owner_item),
            body.version.fields(),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    return _trial_out(result)


@router.post("/{rule_id}/versions/{version_id}/trial", summary="Kayıtlı sürümün deneme hesabı")
async def version_trial(
    rule_id: uuid.UUID, version_id: uuid.UUID, ctx: FinanceRead
) -> BillingTrialOut:
    rule = await _rule(ctx, rule_id)
    version = await _version(ctx, rule, version_id)
    try:
        result = await svc.trial(
            ctx.session,
            svc.RuleInput(rule.name, rule.charge_type_id, rule.owner_item),
            svc.fields_of(version),
            version_id=version.id,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    return _trial_out(result)
