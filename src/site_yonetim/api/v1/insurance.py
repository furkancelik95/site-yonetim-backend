"""Sigorta poliçeleri ve hasar dosyaları — Yazılım Dokümanı OPS-04 (servis isteği 56).

Çekirdek (modül yok). Okuma `expenses.read`, yazma `expenses.manage`. Belgeler
`/subject-documents` ile belge arşivine bağlanır; yanıtta `documents` listesi.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.ops_shared import fields_error, ops_error
from site_yonetim.api.v1.subject_documents import LinkedDocumentOut
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import (
    ClaimStatus,
    OpsError,
    PolicyKind,
    PolicyWarning,
    policy_warning,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Document, InsuranceClaim, InsurancePolicy
from site_yonetim.services import insurance as svc
from site_yonetim.services import subject_documents as docs_svc

router = APIRouter(prefix="/sites/{slug}", tags=["sigorta ve hasar"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_MANAGE))]
LIST_MAX = 500


class PolicyIn(BaseModel):
    kind: str | None = Field(default=None, max_length=20)
    insurer: str | None = Field(default=None, max_length=400)
    policy_no: str | None = Field(default=None, max_length=200)
    coverage: str | None = Field(default=None, max_length=1000)
    premium: Money | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    area: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2000)


class InsurancePolicyOut(BaseModel):
    id: uuid.UUID
    kind: PolicyKind
    insurer: str
    policy_no: str
    coverage: str | None
    premium: Money
    start: dt.date
    end: dt.date
    area: str | None
    note: str | None
    days_left: int = Field(description="bitişe kalan gün (sitenin bugünü); geçmişse eksi")
    warning: PolicyWarning | None = Field(description='"60" · "30" · "expired" · null')
    documents: list[LinkedDocumentOut]

    @classmethod
    def of(
        cls, p: InsurancePolicy, today: dt.date, documents: list[Document]
    ) -> InsurancePolicyOut:
        left = (p.end_date - today).days
        return cls(
            id=p.id,
            kind=PolicyKind(p.kind),
            insurer=p.insurer,
            policy_no=p.policy_no,
            coverage=p.coverage,
            premium=p.premium,
            start=p.start_date,
            end=p.end_date,
            area=p.area,
            note=p.note,
            days_left=left,
            warning=policy_warning(left),
            documents=[LinkedDocumentOut.of(d) for d in documents],
        )


async def _policy_outs(
    ctx: SiteContext, rows: list[InsurancePolicy], today: dt.date
) -> list[InsurancePolicyOut]:
    docs = await docs_svc.for_subjects(ctx.session, "insurance_policies", [p.id for p in rows])
    return [InsurancePolicyOut.of(p, today, docs.get(p.id, [])) for p in rows]


@router.get("/insurance-policies", summary="Poliçeler (bitişe göre)")
async def list_policies(ctx: Reader, today: TodayDep) -> list[InsurancePolicyOut]:
    rows = list(await ctx.session.scalars(svc.policies_query().limit(LIST_MAX)))
    return await _policy_outs(ctx, rows, today)


def _policy_data(body: PolicyIn, current: svc.PolicyData | None) -> svc.PolicyData:
    sent = body.model_fields_set
    if current is None:
        return svc.PolicyData(**{k: getattr(body, k) for k in PolicyIn.model_fields})
    return svc.PolicyData(
        **{k: getattr(body, k) if k in sent else getattr(current, k) for k in PolicyIn.model_fields}
    )


@router.post("/insurance-policies", status_code=status.HTTP_201_CREATED, summary="Poliçe ekle")
async def create_policy(
    body: PolicyIn, ctx: Manager, today: TodayDep
) -> Written[InsurancePolicyOut]:
    try:
        policy = await svc.save_policy(ctx.session, _policy_data(body, None), None)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _policy_outs(ctx, [policy], today))[0]
    await ctx.session.commit()
    return Written(data=out, message="Poliçe kaydedildi; bitişten 60 ve 30 gün önce uyarı gider.")


@router.patch("/insurance-policies/{policy_id}", summary="Poliçeyi düzenle (kısmi)")
async def update_policy(
    policy_id: uuid.UUID, body: PolicyIn, ctx: Manager, today: TodayDep
) -> Written[InsurancePolicyOut]:
    policy = await svc.get_policy(ctx.session, policy_id)
    if policy is None:
        raise NotFoundError("Poliçe bulunamadı.")
    try:
        await svc.save_policy(ctx.session, _policy_data(body, svc.current_policy(policy)), policy)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _policy_outs(ctx, [policy], today))[0]
    await ctx.session.commit()
    return Written(data=out, message="Poliçe güncellendi.")


class ClaimIn(BaseModel):
    policy_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=400, description="ör. talep #12")
    description: str | None = Field(default=None, max_length=1000)
    incident_date: dt.date | None = None
    claimed: Money | None = None


class ClaimPatch(BaseModel):
    status: str | None = Field(
        default=None, max_length=20, description="open · submitted · paid · rejected"
    )
    paid: Money | None = None
    paid_on: dt.date | None = None


class InsuranceClaimOut(BaseModel):
    id: uuid.UUID
    policy_id: uuid.UUID
    policy_label: str
    source: str | None
    description: str
    incident_date: dt.date
    status: ClaimStatus
    claimed: Money | None
    paid: Money | None
    paid_on: dt.date | None
    created_by: str
    created_at: dt.datetime
    documents: list[LinkedDocumentOut]


async def _claim_outs(ctx: SiteContext, rows: list[InsuranceClaim]) -> list[InsuranceClaimOut]:
    policies = await svc.policies_by_id(ctx.session, {c.policy_id for c in rows})
    docs = await docs_svc.for_subjects(ctx.session, "insurance_claims", [c.id for c in rows])
    return [
        InsuranceClaimOut(
            id=c.id,
            policy_id=c.policy_id,
            policy_label=svc.policy_label(policies[c.policy_id])
            if c.policy_id in policies
            else "—",
            source=c.source,
            description=c.description,
            incident_date=c.incident_date,
            status=ClaimStatus(c.status),
            claimed=c.claimed,
            paid=c.paid,
            paid_on=c.paid_on,
            created_by=c.created_by_name,
            created_at=c.created_at,
            documents=[LinkedDocumentOut.of(d) for d in docs.get(c.id, [])],
        )
        for c in rows
    ]


@router.get("/insurance-claims", summary="Hasar dosyaları (yeniden eskiye)")
async def list_claims(ctx: Reader) -> list[InsuranceClaimOut]:
    rows = list(await ctx.session.scalars(svc.claims_query().limit(LIST_MAX)))
    return await _claim_outs(ctx, rows)


@router.post("/insurance-claims", status_code=status.HTTP_201_CREATED, summary="Hasar dosyası aç")
async def create_claim(
    body: ClaimIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[InsuranceClaimOut]:
    policy = await svc.get_policy(ctx.session, body.policy_id) if body.policy_id else None
    try:
        claim = await svc.open_claim(
            ctx.session,
            policy=policy,
            source=body.source,
            description=body.description,
            incident_date=body.incident_date,
            claimed=body.claimed,
            by=current.user.full_name,
            today=today,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    await ctx.session.refresh(claim, ["created_at"])
    out = (await _claim_outs(ctx, [claim]))[0]
    await ctx.session.commit()
    return Written(data=out, message="Hasar dosyası açıldı.")


@router.patch("/insurance-claims/{claim_id}", summary="Hasar dosyası durumu / tazminat")
async def update_claim(
    claim_id: uuid.UUID, body: ClaimPatch, ctx: Manager, today: TodayDep
) -> Written[InsuranceClaimOut]:
    claim = await svc.get_claim(ctx.session, claim_id)
    if claim is None:
        raise NotFoundError("Hasar dosyası bulunamadı.")
    try:
        await svc.update_claim(
            ctx.session,
            claim,
            status=body.status,
            paid=body.paid,
            paid_on=body.paid_on,
            today=today,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    except OpsError as exc:
        raise ops_error(exc) from exc
    await ctx.session.refresh(claim)
    out = (await _claim_outs(ctx, [claim]))[0]
    await ctx.session.commit()
    message = (
        "Tazminat kaydedildi; muhasebe aktarımında gelir kaydı olarak yer alacak."
        if claim.status == ClaimStatus.PAID.value
        else "Hasar dosyası güncellendi."
    )
    return Written(data=out, message=message)
