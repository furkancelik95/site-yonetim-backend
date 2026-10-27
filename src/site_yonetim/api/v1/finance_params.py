"""Finans parametreleri (versiyonlu) — Yazılım Dokümanı FIN-16, §8.7; servis isteği 33.

Okuma `finance.read`; yazma `finance.charge.post`. Finansal etkili değişiklik en erken sonraki
dönemden geçerli (422 `period_locked`, `fields.effective_period`) ve "Finans parametresi" onay
kuralı varsa 202 döner. PM kararlarının ayar noktaları (K9, K25, K23, K2, K11, K24):
`domain/finance_params.py`.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, TodayDep
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import ChargePost, FinanceRead
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error, period_param
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance_params import (
    DEFAULTS,
    FINANCIAL,
    AllocationOrder,
    BankAutoMatch,
    ChargeSource,
    LateFeeMethod,
    Proration,
)
from site_yonetim.domain.fiscal import period_key, period_label
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import FinanceParameterVersion
from site_yonetim.services import finance_params as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/finance-parameters", tags=["finans ayarları"])


class FinanceParamsOut(BaseModel):
    reminder_days: list[int] = Field(description="hatırlatma kademeleri: vade +gün (3 adet)")
    demand_letter_day: int = Field(description="ihtarname hazırlığı: vade +gün")
    proration: Proration = Field(description="K9 kıst: day (gün oranı) · month (ay bazlı)")
    diff_threshold_percent: int = Field(description="tahakkuk fark raporu eşiği (%)")
    bank_auto_match: BankAutoMatch
    plan_tolerance_days: int
    plan_suspends_late_fee: bool
    budget_warn_percent: int
    due_day: int = Field(description="K25 vade günü (ayın kaçı)")
    charge_source: ChargeSource = Field(description="K23 tahakkuk kaynağı")
    late_fee_enabled: bool = Field(
        description="K2 gecikme tazminatı — site kararı, varsayılan kapalı"
    )
    late_fee_monthly_rate_percent: Money = Field(description="aylık oran (%), en fazla 5,00")
    late_fee_method: LateFeeMethod
    late_fee_grace_days: int
    late_fee_cap_percent: int = Field(description="tavan: ana borcun %X'i (0 = yok)")
    late_fee_minimum_amount: Money = Field(description="bu tutarın altındaki gecikme yazılmaz")
    allocation_order: AllocationOrder = Field(description="K24 mahsup sırası")
    debtor_restriction_enabled: bool = Field(
        description="K11 borçlu hizmet kısıtı (varsayılan kapalı)"
    )
    debtor_restriction_min_amount: Money
    debtor_restriction_min_days: int


class FinanceParamsIn(BaseModel):
    """Yalnız değişen alanlar gönderilebilir; gönderilmeyen alan yürürlükteki değerini korur."""

    reminder_days: list[int] | None = None
    demand_letter_day: int | None = None
    proration: str | None = None
    diff_threshold_percent: int | None = None
    bank_auto_match: str | None = None
    plan_tolerance_days: int | None = None
    plan_suspends_late_fee: bool | None = None
    budget_warn_percent: int | None = None
    due_day: int | None = None
    charge_source: str | None = None
    late_fee_enabled: bool | None = None
    late_fee_monthly_rate_percent: Money | None = None
    late_fee_method: str | None = None
    late_fee_grace_days: int | None = None
    late_fee_cap_percent: int | None = None
    late_fee_minimum_amount: Money | None = None
    allocation_order: str | None = None
    debtor_restriction_enabled: bool | None = None
    debtor_restriction_min_amount: Money | None = None
    debtor_restriction_min_days: int | None = None


class FinanceParamVersionOut(BaseModel):
    id: uuid.UUID
    effective_period: str = Field(description="YYYY-MM")
    values: FinanceParamsOut
    changed: list[str]
    status: Literal["active", "approval_pending", "rejected"]
    approval_request_id: uuid.UUID | None
    changed_by: str
    created_at: dt.datetime


class FinanceParametersOut(BaseModel):
    current: FinanceParamsOut
    current_period: str
    earliest_financial_period: str = Field(description="finansal etkili değişiklik en erken")
    defaults: FinanceParamsOut
    financial_keys: list[str] = Field(description="sonraki dönemden geçerli + onaya tabi alanlar")
    versions: list[FinanceParamVersionOut] = Field(description="son 100 sürüm, yeniden eskiye")
    scheduled: list[FinanceParamVersionOut] = Field(
        description="yürürlüğü gelecekte olan aktif sürümler"
    )


def _params(values: dict[str, object]) -> FinanceParamsOut:
    return FinanceParamsOut.model_validate({**DEFAULTS, **values})


def _version(v: FinanceParameterVersion) -> FinanceParamVersionOut:
    return FinanceParamVersionOut(
        id=v.id,
        effective_period=period_key(YearMonth(v.effective_year, v.effective_month)),
        values=_params(v.values),
        changed=list(v.changed),
        status=v.status,  # type: ignore[arg-type]
        approval_request_id=v.approval_request_id,
        changed_by=v.changed_by,
        created_at=v.created_at,
    )


@router.get("", summary="Finans parametreleri: geçerli, varsayılan, sürümler")
async def get_parameters(ctx: FinanceRead, today: TodayDep) -> FinanceParametersOut:
    this_month = YearMonth.of(today)
    versions = await svc.versions(ctx.session)
    return FinanceParametersOut(
        current=_params(await svc.current(ctx.session, today)),
        current_period=period_key(this_month),
        earliest_financial_period=period_key(this_month.next()),
        defaults=_params({}),
        financial_keys=sorted(FINANCIAL),
        versions=[_version(v) for v in versions],
        scheduled=[
            _version(v)
            for v in versions
            if v.status == "active" and YearMonth(v.effective_year, v.effective_month) > this_month
        ],
    )


class FinanceParamVersionIn(BaseModel):
    effective_period: str = Field(description="YYYY-MM")
    values: FinanceParamsIn


def _changes(values: FinanceParamsIn) -> dict[str, object]:
    raw = values.model_dump(exclude_unset=True, exclude_none=True)
    for key in (
        "late_fee_monthly_rate_percent",
        "late_fee_minimum_amount",
        "debtor_restriction_min_amount",
    ):
        if key in raw:
            raw[key] = str(raw[key])
    return raw


@router.post(
    "/versions",
    status_code=HTTPStatus.CREATED,
    summary="Yeni parametre sürümü (finansal etkili + onay kuralı varsa 202)",
    responses={202: {"description": "Onaya gönderildi"}},
)
async def save_version(
    body: FinanceParamVersionIn,
    ctx: ChargePost,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[FinanceParamVersionOut]:
    effective = period_param(body.effective_period, "effective_period")
    try:
        saved = await svc.save(
            ctx.session,
            ctx.site,
            effective=effective,
            changes=_changes(body.values),
            actor=actor(current),
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.NoChangeError as exc:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "no_change", "Değişen bir parametre yok."
        ) from exc
    except svc.EffectiveTooEarlyError as exc:
        label = period_label(exc.earliest)
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "period_locked",
            f"Finansal etkili parametreler en erken {label} döneminden geçerli olur.",
            {"effective_period": f"En erken {label}"},
        ) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    await ctx.session.commit()
    data = _version(saved.version)
    if saved.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=data,
            message=f"Finansal etkili değişiklik onaya gönderildi (talep #{saved.request.number}); "
            f"onaylanınca {period_label(effective)} döneminden geçerli olur.",
        )
    return Written(
        data=data,
        message=f"Finans parametreleri {period_label(effective)} döneminden geçerli olacak "
        "şekilde kaydedildi; geçmiş dönemler değişmez.",
    )
