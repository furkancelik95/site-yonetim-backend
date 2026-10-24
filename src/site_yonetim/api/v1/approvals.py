"""Onay mekanizması — Yazılım Dokümanı ONY-01..03 (docs/06 §2.22).

- Kurallar: okuma `approvals.rules.read` (Yönetici, YK), yazma `approvals.rules.manage`
  (Yönetici). "Onay kuralı değişikliği" için yürürlükte kural varsa değişiklik **onaya gider**
  (202, `status: "approval_pending"`).
- Talepler: karar vermek için izin gerekmez — talebe onaycı olarak atanmış (ya da onaycının
  etkin vekili) olmak yeter. Başkasının talebi "yok"tur (404); kural yöneticisi hepsini görür.
- Yetki devri: kendi rolünü aynı roldeki birine (K20), süreli.

Mevcut akışlar (kayıt başvurusu, iade, tahakkuk, rol değişikliği) henüz bağlı değil (K21); bu
sürümde onaydan geçen tek işlem "onay kuralı değişikliği".
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
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
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission, SiteRole
from site_yonetim.domain.approvals import (
    ACTION_LABELS,
    ActionType,
    ApprovalStatus,
    DayBasis,
    Decision,
    QuorumKind,
    RejectBehavior,
    StepMode,
    quorum_unreachable,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models.approvals import (
    ApprovalAssignment,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalRequest,
    ApprovalRule,
)
from site_yonetim.services import approvals as svc

router = APIRouter(prefix="/sites/{slug}", tags=["onay"])
Paging = Annotated[PageParams, Depends()]
RulesRead = Annotated[SiteContext, Depends(require_permission(Permission.APPROVAL_RULES_READ))]
RulesManage = Annotated[SiteContext, Depends(require_permission(Permission.APPROVAL_RULES_MANAGE))]


def _error(exc: svc.ApprovalError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def _person(current: CurrentUserDep) -> svc.Person:
    return svc.Person(current.user.id, current.user.full_name)


async def _staff(ctx: SiteContextDep) -> SiteContext:
    if ctx.access.person_id is not None:
        raise ForbiddenError("Onay ekranları personel içindir.")
    return ctx


Staff = Annotated[SiteContext, Depends(_staff)]


# --- İşlem tipleri ve kurallar ---------------------------------------------------------


class ActionTypeOut(BaseModel):
    key: ActionType
    label: str
    connected: bool = Field(description="true → bu tipteki işlemler şu an onaydan geçiyor")


@router.get("/approval-action-types", summary="Onay kuralı tanımlanabilecek işlem tipleri")
async def action_types(ctx: RulesRead) -> list[ActionTypeOut]:
    connected = svc.supported_actions()
    return [
        ActionTypeOut(key=a, label=ACTION_LABELS[a], connected=a in connected) for a in ActionType
    ]


class StepIn(BaseModel):
    roles: list[SiteRole] = Field(min_length=1, max_length=6)
    quorum: QuorumKind = Field(description="any (1/N) · all (N/N) · majority · count (M)")
    count: int | None = Field(default=None, ge=1, le=50, description="yalnız quorum=count")


class RuleIn(BaseModel):
    action_type: ActionType
    steps: list[StepIn] = Field(min_length=1, max_length=5)
    mode: StepMode = StepMode.SEQUENTIAL
    reject_behavior: RejectBehavior = RejectBehavior.SINGLE_REJECTS
    amount_min: Money | None = Field(default=None, description="hariç (tutar > alt)")
    amount_max: Money | None = Field(default=None, description="dahil (tutar ≤ üst)")
    source: str | None = Field(default=None, max_length=40, description='ör. "emergency"')
    remind_after_days: int = Field(ge=1, le=30)
    escalate_after_days: int = Field(ge=2, le=365)
    day_basis: DayBasis = DayBasis.CALENDAR
    escalation_role: SiteRole | None = Field(
        default=None, description="boşsa yalnız hatırlatma gider (rol sırası tanımsız: K20)"
    )
    effective_from: dt.date
    basis_note: str | None = Field(
        default=None, max_length=500, description="dayanak, ör. YK kararı"
    )


class ProposedRuleOut(RuleIn):
    """Kural değişikliği talebinin önerdiği kural — `RuleIn` biçiminde, forma olduğu gibi dolar."""


class RuleChangeOut(BaseModel):
    op: Literal["create", "update", "archive"]
    rule_id: uuid.UUID | None = Field(description="güncellenen/arşivlenen kural; yeni kuralda null")
    rule: ProposedRuleOut | None = Field(description="önerilen kural; arşivlemede null")


def _rule_change(request: ApprovalRequest) -> RuleChangeOut | None:
    payload = request.payload
    if ActionType(request.action_type) is not ActionType.APPROVAL_RULE_CHANGE or not payload:
        return None
    data = payload.get("data")
    return RuleChangeOut(
        op=payload.get("op", "create"),
        rule_id=payload.get("rule_id"),
        rule=ProposedRuleOut.model_validate(data) if data else None,
    )


def _rule_data(body: RuleIn) -> svc.RuleData:
    return svc.RuleData(
        action_type=body.action_type.value,
        steps=[s.model_dump(mode="json") for s in body.steps],
        mode=body.mode.value,
        reject_behavior=body.reject_behavior.value,
        amount_min=body.amount_min,
        amount_max=body.amount_max,
        source=body.source,
        remind_after_days=body.remind_after_days,
        escalate_after_days=body.escalate_after_days,
        day_basis=body.day_basis.value,
        escalation_role=body.escalation_role.value if body.escalation_role else None,
        effective_from=body.effective_from,
        basis_note=body.basis_note,
    )


class StepOut(BaseModel):
    roles: list[SiteRole]
    quorum: QuorumKind
    count: int | None
    eligible: int = Field(description="bu roldeki aktif kullanıcı sayısı (N)")
    required: int = Field(description="gereken onay sayısı (M)")


class VersionOut(BaseModel):
    version: int
    effective_from: dt.date
    amount_min: Money | None
    amount_max: Money | None
    source: str | None
    mode: StepMode
    reject_behavior: RejectBehavior
    remind_after_days: int
    escalate_after_days: int
    day_basis: DayBasis
    escalation_role: SiteRole | None
    steps: list[StepOut]
    basis_note: str | None
    created_by: str | None
    created_at: dt.datetime


class RuleOut(BaseModel):
    id: uuid.UUID
    action_type: ActionType
    action_label: str
    is_active: bool
    is_template: bool = Field(description='"Önerilen" hazır şablondan, henüz düzenlenmedi')
    applicable: bool = Field(description="false → bir adımın rolünde aktif kullanıcı kalmadı")
    current: VersionOut = Field(description="en son versiyon (ileri tarihli olabilir)")
    in_force_version: int | None = Field(description="bugün yürürlükteki versiyon")


async def _rule_out(
    ctx: SiteContext, rule: ApprovalRule, today: dt.date, counts: dict[SiteRole, int]
) -> RuleOut:
    latest = await svc.latest_version(ctx.session, rule)
    in_force = await svc.effective_version(ctx.session, rule, today)
    steps = []
    for raw in latest.steps:
        step = svc.snapshot_steps({"steps": [raw]})[0]
        n = sum(counts.get(r, 0) for r in step.roles)
        steps.append(
            StepOut(
                roles=list(step.roles),
                quorum=step.quorum.kind,
                count=step.quorum.count,
                eligible=n,
                required=step.quorum.required(n),
            )
        )
    return RuleOut(
        id=rule.id,
        action_type=ActionType(rule.action_type),
        action_label=ACTION_LABELS[ActionType(rule.action_type)],
        is_active=rule.is_active,
        is_template=rule.is_template,
        applicable=all(s.eligible > 0 for s in steps),
        current=VersionOut(
            version=latest.version,
            effective_from=latest.effective_from,
            amount_min=latest.amount_min,
            amount_max=latest.amount_max,
            source=latest.source,
            mode=StepMode(latest.mode),
            reject_behavior=RejectBehavior(latest.reject_behavior),
            remind_after_days=latest.remind_after_days,
            escalate_after_days=latest.escalate_after_days,
            day_basis=DayBasis(latest.day_basis),
            escalation_role=SiteRole(latest.escalation_role) if latest.escalation_role else None,
            steps=steps,
            basis_note=latest.basis_note,
            created_by=latest.created_by_name,
            created_at=latest.created_at,
        ),
        in_force_version=in_force.version if in_force else None,
    )


@router.get("/approval-rules", summary="Onay kuralları (işlem tipine göre)")
async def list_rules(
    ctx: RulesRead,
    today: TodayDep,
    archived: Annotated[bool, Query(description="true → arşivdekiler")] = False,
) -> list[RuleOut]:
    counts = await svc._holder_counts(ctx.session, ctx.site)
    rules = await ctx.session.scalars(
        select(ApprovalRule)
        .where(ApprovalRule.is_active.is_(not archived))
        .order_by(ApprovalRule.action_type, ApprovalRule.created_at)
    )
    return [await _rule_out(ctx, r, today, counts) for r in rules]


class RuleWriteOut(BaseModel):
    status: Literal["saved", "approval_pending"]
    rule: RuleOut | None
    approval_request_id: uuid.UUID | None
    warnings: list[str] = Field(description='ör. "10.000,00 TL üstü için kural yok; varsayılan…"')


async def _write_out(
    ctx: SiteContext, result: svc.RuleWriteResult, today: dt.date, response: Response
) -> Written[RuleWriteOut]:
    await ctx.session.commit()
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=RuleWriteOut(
                status="approval_pending",
                rule=None,
                approval_request_id=result.request.id,
                warnings=result.warnings,
            ),
            message="Kural değişikliği onaya gönderildi; onaylanınca yürürlüğe girer.",
        )
    if result.rule is None:  # pragma: no cover
        raise RuntimeError("Kural yazılmadı")
    counts = await svc._holder_counts(ctx.session, ctx.site)
    out = await _rule_out(ctx, result.rule, today, counts)
    if not result.rule.is_active:
        message = (
            "Kural arşive kaldırıldı; yeni talepler diğer kurallara ya da varsayılana göre ilerler."
        )
    else:
        message = (
            f"Kural kaydedildi (versiyon {out.current.version}); "
            "bekleyen talepler eski versiyonla sürer."
        )
    return Written(
        data=RuleWriteOut(
            status="saved", rule=out, approval_request_id=None, warnings=result.warnings
        ),
        message=message,
    )


@router.post("/approval-rules", status_code=HTTPStatus.CREATED, summary="Onay kuralı ekle")
async def create_rule(
    body: RuleIn,
    response: Response,
    ctx: RulesManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[RuleWriteOut]:
    try:
        result = await svc.save_rule(
            ctx.session, ctx.site, _rule_data(body), rule=None, actor=_person(current),
            today=today, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    return await _write_out(ctx, result, today, response)


async def _rule(ctx: SiteContext, rule_id: uuid.UUID) -> ApprovalRule:
    rule = await ctx.session.scalar(
        select(ApprovalRule).where(ApprovalRule.id == rule_id).with_for_update()
    )
    if rule is None:
        raise NotFoundError("Kural bulunamadı.")
    return rule


@router.put("/approval-rules/{rule_id}", summary="Kuralı düzenle (yeni versiyon)")
async def update_rule(
    rule_id: uuid.UUID,
    body: RuleIn,
    response: Response,
    ctx: RulesManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[RuleWriteOut]:
    rule = await _rule(ctx, rule_id)
    if not rule.is_active:
        raise ApiError(HTTPStatus.CONFLICT, "rule_archived", "Arşivdeki kural düzenlenemez.")
    try:
        result = await svc.save_rule(
            ctx.session, ctx.site, _rule_data(body), rule=rule, actor=_person(current),
            today=today, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    return await _write_out(ctx, result, today, response)


@router.post("/approval-rules/{rule_id}/archive", summary="Kuralı arşive kaldır")
async def archive_rule(
    rule_id: uuid.UUID,
    response: Response,
    ctx: RulesManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[RuleWriteOut]:
    rule = await _rule(ctx, rule_id)
    try:
        result = await svc.archive_rule(
            ctx.session, ctx.site, rule, actor=_person(current), today=today, now=now
        )
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    return await _write_out(ctx, result, today, response)


class RestoreOut(BaseModel):
    created: list[RuleOut]
    skipped: list[str]


@router.post("/approval-rules/restore-defaults", summary='"Önerilen" hazır şablonları kur')
async def restore_defaults(
    ctx: RulesManage, current: CurrentUserDep, today: TodayDep
) -> Written[RestoreOut]:
    result = await svc.restore_defaults(ctx.session, ctx.site, actor=_person(current), today=today)
    await ctx.session.commit()
    counts = await svc._holder_counts(ctx.session, ctx.site)
    created = [await _rule_out(ctx, r, today, counts) for r in result.created]
    message = (
        f"{len(created)} hazır şablon kuruldu."
        if created
        else "Kurulacak şablon yok: şablonu olan işlem tiplerinin hepsinde zaten kural var."
    )
    return Written(data=RestoreOut(created=created, skipped=result.skipped), message=message)


class SimulateIn(BaseModel):
    action_type: ActionType
    amount: Money | None = None
    source: str | None = Field(default=None, max_length=40)


class SimulatedStep(BaseModel):
    roles: list[SiteRole]
    required: int
    eligible: int
    approvers: list[str]


class SimulateOut(BaseModel):
    rule_id: uuid.UUID | None = Field(description="null → varsayılan kural (site yöneticisi 1/1)")
    version: int | None
    mode: StepMode
    steps: list[SimulatedStep]


@router.post("/approval-rules/simulate", summary="Bu işlem için hangi kural uygulanır?")
async def simulate(body: SimulateIn, ctx: RulesRead, today: TodayDep) -> SimulateOut:
    version = await svc.match_rule(
        ctx.session, body.action_type, amount=body.amount, source=body.source, today=today
    )
    snapshot = svc.snapshot_of(version)
    holders = await svc._role_holders(ctx.session, ctx.site, tuple(SiteRole))
    steps = []
    for step in svc.snapshot_steps(snapshot):
        names = [name for _, name, role in holders if role in step.roles]
        steps.append(
            SimulatedStep(
                roles=list(step.roles),
                required=step.quorum.required(len(names)),
                eligible=len(names),
                approvers=sorted(names),
            )
        )
    return SimulateOut(
        rule_id=version.rule_id if version else None,
        version=version.version if version else None,
        mode=StepMode(snapshot["mode"]),
        steps=steps,
    )


# --- Talepler (onay kutusu) ------------------------------------------------------------


class ApproverOut(BaseModel):
    user_id: uuid.UUID
    name: str
    role: str
    source: Literal["rule", "escalation"]
    decision: Decision | None
    decided_by: str | None = Field(description="vekâleten verildiyse kararı veren")
    comment: str | None
    decided_at: dt.datetime | None


class RequestStepOut(BaseModel):
    index: int
    roles: list[SiteRole]
    required: int | None
    eligible: int | None
    approvals: int
    rejections: int
    status: Literal["waiting", "open", "approved", "rejected"]
    quorum_unreachable: bool = Field(description="kalanların hepsi onaylasa da M'ye ulaşılamaz")
    approvers: list[ApproverOut]


class DecisionOut(BaseModel):
    revision: int
    step_index: int
    user_name: str
    on_behalf_of: str | None = Field(description='vekâleten: asıl onaycı ("vekâleten: Selin")')
    decision: Decision
    comment: str | None
    at: dt.datetime


class ApprovalRequestOut(BaseModel):
    id: uuid.UUID
    number: int
    action_type: ActionType
    action_label: str
    title: str
    summary: dict[str, Any]
    amount: Money | None
    status: ApprovalStatus
    revision: int
    requested_by: str
    requested_by_id: uuid.UUID
    created_at: dt.datetime
    decided_at: dt.datetime | None
    failure_reason: str | None
    post_approval: bool = Field(description='"sonradan onay" etiketi (acil gider)')
    escalated: bool
    remind_on: dt.date | None
    escalate_on: dt.date | None
    progress: str = Field(description='ör. "2/3 onay" — açık adım için')
    can_decide: bool = Field(description="oturumdaki kişi şimdi karar verebilir mi")
    acting_for: str | None = Field(description="vekâleten karar verecekse asıl onaycı")
    can_withdraw: bool
    can_resubmit: bool
    steps: list[RequestStepOut]
    history: list[DecisionOut] = Field(description="tüm sürümlerin kararları")
    rule_change: RuleChangeOut | None = Field(
        default=None,
        description="yalnız approval_rule_change: talebin önerdiği değişiklik (son sürüm); "
        "yeniden gönderme formu bununla dolar",
    )


class RequestListItem(BaseModel):
    id: uuid.UUID
    number: int
    action_type: ActionType
    action_label: str
    title: str
    amount: Money | None
    status: ApprovalStatus
    requested_by: str
    created_at: dt.datetime
    progress: str
    post_approval: bool
    escalated: bool
    remind_on: dt.date | None
    escalate_on: dt.date | None


async def _steps_out(
    ctx: SiteContext, request: ApprovalRequest
) -> tuple[list[RequestStepOut], list[DecisionOut], str]:
    assignments = list(
        await ctx.session.scalars(
            select(ApprovalAssignment)
            .where(
                ApprovalAssignment.request_id == request.id,
                ApprovalAssignment.revision == request.revision,
            )
            .order_by(ApprovalAssignment.step_index, ApprovalAssignment.user_name)
        )
    )
    decisions = list(
        await ctx.session.scalars(
            select(ApprovalDecision)
            .where(ApprovalDecision.request_id == request.id)
            .order_by(ApprovalDecision.created_at)
        )
    )
    current = [d for d in decisions if d.revision == request.revision]
    by_slot = {d.slot_user_id: d for d in current}
    open_indexes = set(svc._open_indexes(request))
    steps: list[RequestStepOut] = []
    progress = ""
    for index, raw in enumerate(request.snapshot["steps"]):
        mine = [a for a in assignments if a.step_index == index]
        approvals = sum(1 for d in current if d.step_index == index and d.decision == "approve")
        rejections = sum(1 for d in current if d.step_index == index and d.decision == "reject")
        outcome = raw.get("outcome")
        status: Literal["waiting", "open", "approved", "rejected"] = (
            outcome
            if outcome in ("approved", "rejected")
            else "open"
            if index in open_indexes
            else "waiting"
        )
        required = raw.get("required")
        eligible = raw.get("eligible")
        if status == "open" and required is not None:
            progress = f"{approvals}/{required} onay"
        steps.append(
            RequestStepOut(
                index=index,
                roles=[SiteRole(r) for r in raw["roles"]],
                required=required,
                eligible=eligible,
                approvals=approvals,
                rejections=rejections,
                status=status,
                quorum_unreachable=status == "open"
                and required is not None
                and quorum_unreachable(
                    approvals=approvals,
                    rejections=rejections,
                    eligible=len(mine),
                    required=required,
                ),
                approvers=[
                    ApproverOut(
                        user_id=a.user_id,
                        name=a.user_name,
                        role=a.role,
                        source=a.source,  # type: ignore[arg-type]
                        decision=Decision(by_slot[a.user_id].decision)
                        if a.user_id in by_slot
                        else None,
                        decided_by=by_slot[a.user_id].user_name
                        if a.user_id in by_slot and by_slot[a.user_id].on_behalf_of_name
                        else None,
                        comment=by_slot[a.user_id].comment if a.user_id in by_slot else None,
                        decided_at=by_slot[a.user_id].created_at if a.user_id in by_slot else None,
                    )
                    for a in mine
                ],
            )
        )
    history = [
        DecisionOut(
            revision=d.revision,
            step_index=d.step_index,
            user_name=d.user_name,
            on_behalf_of=d.on_behalf_of_name,
            decision=Decision(d.decision),
            comment=d.comment,
            at=d.created_at,
        )
        for d in decisions
    ]
    return steps, history, progress


def _status_text(request: ApprovalRequest) -> str:
    labels = {
        ApprovalStatus.APPROVED: "Onaylandı",
        ApprovalStatus.EXECUTED: "Onaylandı",
        ApprovalStatus.EXECUTION_FAILED: "Onaylandı – yürütülemedi",
        ApprovalStatus.REJECTED: "Reddedildi",
        ApprovalStatus.WITHDRAWN: "Geri çekildi",
        ApprovalStatus.REVISION_REQUESTED: "Revizyon istendi",
        ApprovalStatus.MODULE_DISABLED: "Modül kapalı",
    }
    return labels.get(ApprovalStatus(request.status), "")


async def _request_out(
    ctx: SiteContext, request: ApprovalRequest, user_id: uuid.UUID, now: dt.datetime
) -> ApprovalRequestOut:
    steps, history, progress = await _steps_out(ctx, request)
    slot = await svc.find_slot(ctx.session, request, user_id, now)
    remind_on, escalate_on = svc.due_dates(request)
    return ApprovalRequestOut(
        id=request.id,
        number=request.number,
        action_type=ActionType(request.action_type),
        action_label=ACTION_LABELS[ActionType(request.action_type)],
        title=request.title,
        summary=request.summary,
        amount=request.amount,
        status=ApprovalStatus(request.status),
        revision=request.revision,
        requested_by=request.requested_by_name,
        requested_by_id=request.requested_by,
        created_at=request.created_at,
        decided_at=request.decided_at,
        failure_reason=request.failure_reason,
        post_approval=request.post_approval,
        escalated=request.escalated_at is not None,
        remind_on=remind_on if ApprovalStatus(request.status) is ApprovalStatus.PENDING else None,
        escalate_on=escalate_on
        if ApprovalStatus(request.status) is ApprovalStatus.PENDING
        else None,
        progress=progress or _status_text(request),
        can_decide=slot is not None and user_id != request.requested_by,
        acting_for=slot.assignment.user_name if slot and slot.delegation else None,
        can_withdraw=request.requested_by == user_id
        and ApprovalStatus(request.status)
        in (ApprovalStatus.PENDING, ApprovalStatus.REVISION_REQUESTED),
        can_resubmit=request.requested_by == user_id
        and ApprovalStatus(request.status) is ApprovalStatus.REVISION_REQUESTED,
        steps=steps,
        history=history,
        rule_change=_rule_change(request),
    )


async def _list_item(ctx: SiteContext, request: ApprovalRequest) -> RequestListItem:
    _, _, progress = await _steps_out(ctx, request)
    remind_on, escalate_on = svc.due_dates(request)
    pending = ApprovalStatus(request.status) is ApprovalStatus.PENDING
    return RequestListItem(
        id=request.id,
        number=request.number,
        action_type=ActionType(request.action_type),
        action_label=ACTION_LABELS[ActionType(request.action_type)],
        title=request.title,
        amount=request.amount,
        status=ApprovalStatus(request.status),
        requested_by=request.requested_by_name,
        created_at=request.created_at,
        progress=progress or _status_text(request),
        post_approval=request.post_approval,
        escalated=request.escalated_at is not None,
        remind_on=remind_on if pending else None,
        escalate_on=escalate_on if pending else None,
    )


@router.get("/approvals", summary="Onay kutusu: bekleyenler · taleplerim · geçmiş")
async def list_requests(
    ctx: Staff,
    current: CurrentUserDep,
    paging: Paging,
    now: NowDep,
    tab: Annotated[Literal["pending", "mine", "history"], Query()] = "pending",
) -> Page[RequestListItem]:
    if tab == "pending":
        delegators = [
            d.from_user_id for d in await svc.delegators_for(ctx.session, current.user.id, now)
        ]
        query = svc.pending_for(current.user.id, delegators)
    elif tab == "mine":
        query = svc.mine(current.user.id)
    else:
        query = svc.history_for(current.user.id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=[await _list_item(ctx, r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _visible(
    ctx: SiteContext,
    request_id: uuid.UUID,
    user_id: uuid.UUID,
    now: dt.datetime,
    *,
    lock: bool = False,
) -> ApprovalRequest:
    request = await svc.get_request(ctx.session, request_id, lock=lock)
    oversight = ctx.access.can(Permission.APPROVAL_RULES_MANAGE)
    if request is None or not await svc.can_view(
        ctx.session, request, user_id, oversight=oversight, now=now
    ):
        raise NotFoundError("Onay talebi bulunamadı.")
    return request


@router.get("/approvals/{request_id}", summary="Onay talebi ayrıntısı ve zinciri")
async def get_request(
    request_id: uuid.UUID, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> ApprovalRequestOut:
    request = await _visible(ctx, request_id, current.user.id, now)
    return await _request_out(ctx, request, current.user.id, now)


class ApprovalCommentIn(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalReasonIn(BaseModel):
    comment: str = Field(max_length=1000, description="gerekçe (zorunlu)")


async def _decide(
    ctx: SiteContext,
    current: CurrentUserDep,
    request_id: uuid.UUID,
    decision: Decision,
    comment: str | None,
    now: dt.datetime,
) -> Written[ApprovalRequestOut]:
    request = await _visible(ctx, request_id, current.user.id, now, lock=True)
    try:
        await svc.decide(
            ctx.session, ctx.site, request, decider=_person(current), decision=decision,
            comment=comment, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    out = await _request_out(ctx, request, current.user.id, now)
    messages = {
        ApprovalStatus.EXECUTED: "Onaylandı ve işlem yürütüldü.",
        ApprovalStatus.EXECUTION_FAILED: (
            "Onaylandı ama işlem yürütülemedi; talep edene bildirildi."
        ),
        ApprovalStatus.REJECTED: "Talep reddedildi; talep edene gerekçeyle bildirildi.",
        ApprovalStatus.REVISION_REQUESTED: "Revizyon istendi; talep edene bildirildi.",
    }
    message = messages.get(out.status, "Kararınız kaydedildi.")
    return Written(data=out, message=message)


@router.post("/approvals/{request_id}/approve", summary="Onayla")
async def approve(
    request_id: uuid.UUID, body: ApprovalCommentIn, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[ApprovalRequestOut]:
    return await _decide(ctx, current, request_id, Decision.APPROVE, body.comment, now)


@router.post("/approvals/{request_id}/reject", summary="Reddet (gerekçe zorunlu)")
async def reject(
    request_id: uuid.UUID, body: ApprovalReasonIn, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[ApprovalRequestOut]:
    return await _decide(ctx, current, request_id, Decision.REJECT, body.comment, now)


@router.post("/approvals/{request_id}/request-revision", summary="Revizyon iste (gerekçe zorunlu)")
async def request_revision(
    request_id: uuid.UUID, body: ApprovalReasonIn, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[ApprovalRequestOut]:
    return await _decide(ctx, current, request_id, Decision.REVISION, body.comment, now)


@router.post("/approvals/{request_id}/withdraw", summary="Talebimi geri çek")
async def withdraw(
    request_id: uuid.UUID, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[ApprovalRequestOut]:
    request = await _visible(ctx, request_id, current.user.id, now, lock=True)
    try:
        await svc.withdraw(ctx.session, request, user_id=current.user.id, now=now)
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _request_out(ctx, request, current.user.id, now), message="Talep geri çekildi."
    )


class ResubmitIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    amount: Money | None = None
    rule: RuleIn | None = Field(default=None, description="onay kuralı değişikliğinde: yeni öneri")


@router.post("/approvals/{request_id}/resubmit", summary="Revizyondan sonra yeniden gönder")
async def resubmit(
    request_id: uuid.UUID,
    body: ResubmitIn,
    ctx: Staff,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ApprovalRequestOut]:
    request = await _visible(ctx, request_id, current.user.id, now, lock=True)
    payload = None
    if ActionType(request.action_type) is ActionType.APPROVAL_RULE_CHANGE:
        if body.rule is None:
            raise form_error(FormFieldError({"rule": "Kuralın düzeltilmiş hâlini gönderin."}))
        previous = request.payload or {}
        rule_id = uuid.UUID(previous["rule_id"]) if previous.get("rule_id") else None
        rule = await ctx.session.get(ApprovalRule, rule_id) if rule_id else None
        try:
            await svc.check_rule(
                ctx.session, ctx.site, _rule_data(body.rule), today=today,
                exclude_rule_id=rule.id if rule else None,
            )  # fmt: skip
        except FormFieldError as exc:
            raise form_error(exc) from exc
        payload = svc._rule_payload(previous.get("op", "create"), rule_id, _rule_data(body.rule))
    try:
        await svc.resubmit(
            ctx.session, ctx.site, request, user_id=current.user.id, title=body.title,
            summary=None, payload=payload, amount=body.amount, now=now,
        )  # fmt: skip
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _request_out(ctx, request, current.user.id, now),
        message="Talep yeniden gönderildi; onay zinciri baştan başladı.",
    )


# --- Yetki devri ---------------------------------------------------------------------------


class DelegationIn(BaseModel):
    to_user_id: uuid.UUID
    roles: list[SiteRole] = Field(min_length=1, max_length=1)
    starts_at: dt.datetime
    ends_at: dt.datetime
    note: str | None = Field(default=None, max_length=500)


class DelegationOut(BaseModel):
    id: uuid.UUID
    from_user_id: uuid.UUID
    from_name: str
    to_user_id: uuid.UUID
    to_name: str
    roles: list[SiteRole]
    starts_at: dt.datetime
    ends_at: dt.datetime
    ended_at: dt.datetime | None
    note: str | None
    state: Literal["scheduled", "active", "ended"]
    direction: Literal["given", "received"]

    @classmethod
    def of(cls, d: ApprovalDelegation, me: uuid.UUID, now: dt.datetime) -> DelegationOut:
        ended = d.ended_at is not None or d.ends_at <= now
        state: Literal["scheduled", "active", "ended"] = (
            "ended" if ended else "active" if d.starts_at <= now else "scheduled"
        )
        return cls(
            id=d.id,
            from_user_id=d.from_user_id,
            from_name=d.from_name,
            to_user_id=d.to_user_id,
            to_name=d.to_name,
            roles=[SiteRole(r) for r in d.roles],
            starts_at=d.starts_at,
            ends_at=d.ends_at,
            ended_at=d.ended_at,
            note=d.note,
            state=state,
            direction="given" if d.from_user_id == me else "received",
        )


@router.get("/delegations", summary="Yetki devirlerim (verdiklerim ve aldıklarım)")
async def list_delegations(ctx: Staff, current: CurrentUserDep, now: NowDep) -> list[DelegationOut]:
    me = current.user.id
    rows = await ctx.session.scalars(
        select(ApprovalDelegation)
        .where((ApprovalDelegation.from_user_id == me) | (ApprovalDelegation.to_user_id == me))
        .order_by(ApprovalDelegation.starts_at.desc())
        .limit(100)
    )
    return [DelegationOut.of(d, me, now) for d in rows]


class CandidateOut(BaseModel):
    user_id: uuid.UUID
    name: str
    role: SiteRole


@router.get("/delegations/candidates", summary="Yetkimi devredebileceğim kişiler (aynı rol)")
async def delegation_candidates(ctx: Staff, current: CurrentUserDep) -> list[CandidateOut]:
    holders = await svc._role_holders(ctx.session, ctx.site, (ctx.access.role,))
    return [CandidateOut(user_id=u, name=n, role=r) for u, n, r in holders if u != current.user.id]


@router.post("/delegations", status_code=HTTPStatus.CREATED, summary="Yetki devri tanımla")
async def create_delegation(
    body: DelegationIn, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[DelegationOut]:
    try:
        delegation = await svc.create_delegation(
            ctx.session, ctx.site, delegator=_person(current), to_user_id=body.to_user_id,
            roles=[r.value for r in body.roles], starts_at=body.starts_at, ends_at=body.ends_at,
            note=body.note, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    out = DelegationOut.of(delegation, current.user.id, now)
    return Written(
        data=out,
        message=f"Onay yetkiniz {delegation.to_name} kişisine {delegation.ends_at:%d.%m.%Y %H:%M} "
        "tarihine kadar devredildi.",
    )


@router.post("/delegations/{delegation_id}/end", summary="Devri erken sonlandır")
async def end_delegation(
    delegation_id: uuid.UUID, ctx: Staff, current: CurrentUserDep, now: NowDep
) -> Written[DelegationOut]:
    delegation = await ctx.session.get(ApprovalDelegation, delegation_id)
    if delegation is None:
        raise NotFoundError("Devir bulunamadı.")
    try:
        await svc.end_delegation(ctx.session, delegation, user_id=current.user.id, now=now)
    except svc.ApprovalError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=DelegationOut.of(delegation, current.user.id, now), message="Devir sonlandırıldı."
    )
