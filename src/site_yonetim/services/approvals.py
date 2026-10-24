"""Onay servisi — Yazılım Dokümanı Bölüm 6 (ONY-01..03). Açık site kapsamında; transaction'ı
çağıran yönetir.

Modül onay gerektiren komutu doğrudan yürütmez: `request_approval()` çağırır, komut "onay
bekliyor" olarak etkisiz kalır; onay tamamlanınca servis modülün kaydettiği **yürütücüyü** aynı
transaction'da çağırır (`register_executor`). Yürütücü veri artık geçersizse `ExecutionError`
fırlatır → "onaylandı – yürütülemedi".

Kurallar (6.2–6.5, ONY-01..03):
- Talep açıldığı andaki kural versiyonuyla ilerler (`snapshot`); kural değişikliği bekleyen
  talebi etkilemez.
- Talep eden onaycı listesinden çıkar (N azalır). Aynı kişi bir talepte bir kez karar verir.
- Ret ve revizyonda gerekçe zorunlu. Revizyonda talep eden düzenleyip yeniden gönderir: zincir
  baştan başlar, eski kararlar geçmişte kalır. Talep eden onay bitmeden geri çekebilir.
- Hatırlatma X gün, eskalasyon Y gün sonra (gece işi); **otomatik onay/ret yok**. Eskalasyon
  hedefi kuralda seçilmemişse yalnız hatırlatma gider (K20).
- Yetki devri: aynı role sahip kişiye (K20), aynı rol için tek aktif devir, zincirleme yok;
  devralan, devredenin kendi talebini onaylayamaz; kararlar "vekâleten" yazılır.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope, site_scope
from site_yonetim.domain.access import SiteRole
from site_yonetim.domain.approvals import (
    ACTION_LABELS,
    DEFAULT_STEPS,
    OPEN_STATUSES,
    STEPS_MAX,
    ActionType,
    ApprovalStatus,
    Condition,
    DayBasis,
    Decision,
    Quorum,
    QuorumKind,
    RejectBehavior,
    Step,
    StepMode,
    StepOutcome,
    add_days,
    check_timing,
    coverage_gaps,
    ranges_overlap,
    step_outcome,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import Site
from site_yonetim.models.approvals import (
    ApprovalAssignment,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalRequest,
    ApprovalRule,
    ApprovalRuleVersion,
)
from site_yonetim.models.audit import Actor, ActorType, current_actor, set_actor
from site_yonetim.services import members as members_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.charging import BUSINESS_TZ

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
APPROVER_ROLES = frozenset(r for r in SiteRole if r is not SiteRole.RESIDENT)
COMMENT_MAX, TITLE_MAX = 1000, 200


class ApprovalError(Exception):
    """İş kuralı ihlali: `code` makine için, mesaj Türkçe. `status`: 403/404/409."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ExecutionError(Exception):
    """Yürütücü: onaylandı ama komut artık uygulanamıyor (ör. tedarikçi pasif)."""


@dataclass(frozen=True, slots=True)
class Person:
    user_id: uuid.UUID
    name: str


# --- Yürütücü kaydı -----------------------------------------------------------------------

Executor = Callable[[AsyncSession, ApprovalRequest], Awaitable[None]]
_EXECUTORS: dict[ActionType, Executor] = {}


def register_executor(action: ActionType, executor: Executor) -> None:
    """Modül, onaylanınca çalışacak komutunu kaydeder (uygulama açılışında, bir kez)."""
    _EXECUTORS[action] = executor


def supported_actions() -> frozenset[ActionType]:
    return frozenset(_EXECUTORS)


# --- Kural verisi ve doğrulama ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleData:
    action_type: str | None
    steps: list[dict[str, Any]] | None
    mode: str | None = StepMode.SEQUENTIAL.value
    reject_behavior: str | None = RejectBehavior.SINGLE_REJECTS.value
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    source: str | None = None
    remind_after_days: int | None = None
    escalate_after_days: int | None = None
    day_basis: str | None = DayBasis.CALENDAR.value
    escalation_role: str | None = None
    effective_from: date | None = None
    basis_note: str | None = None


def _steps(raw: list[dict[str, Any]] | None, errors: dict[str, str]) -> list[Step]:
    if not raw or len(raw) > STEPS_MAX:
        errors["steps"] = f"1 ile {STEPS_MAX} arasında onay adımı tanımlayın."
        return []
    steps: list[Step] = []
    for index, item in enumerate(raw, start=1):
        key = f"steps.{index}"
        try:
            roles = tuple(SiteRole(r) for r in item.get("roles") or [])
            kind = QuorumKind(item.get("quorum") or "")
        except ValueError:
            errors[key] = f"{index}. adımda rol ve gereken onay türünü seçin."
            continue
        if not roles or any(r not in APPROVER_ROLES for r in roles):
            errors[key] = f"{index}. adımda en az bir personel rolü seçin."
            continue
        count = item.get("count")
        if kind is QuorumKind.COUNT and (not isinstance(count, int) or count < 1):
            errors[key] = f"{index}. adımda gereken onay sayısını yazın (en az 1)."
            continue
        steps.append(Step(roles, Quorum(kind, count if kind is QuorumKind.COUNT else None)))
    return steps


@dataclass(frozen=True, slots=True)
class CheckedRule:
    action: ActionType
    condition: Condition
    steps: list[Step]
    mode: StepMode
    reject_behavior: RejectBehavior
    remind_after_days: int
    escalate_after_days: int
    day_basis: DayBasis
    escalation_role: SiteRole | None
    effective_from: date
    basis_note: str | None
    warnings: list[str] = field(default_factory=list)


async def _holder_counts(session: AsyncSession, site: Site) -> dict[SiteRole, int]:
    counts: dict[SiteRole, int] = {}
    for m in await members_svc.list_members(session, site):
        if m.is_active and m.user.is_active:
            counts[m.role] = counts.get(m.role, 0) + 1
    return counts


async def check_rule(
    session: AsyncSession,
    site: Site,
    data: RuleData,
    *,
    today: date,
    exclude_rule_id: uuid.UUID | None = None,
) -> CheckedRule:
    """ONY-01 iş akışı 2–3: alan, çakışma, rolde aktif kullanıcı ve M ≤ N denetimi."""
    errors: dict[str, str] = {}
    action: ActionType | None = None
    try:
        action = ActionType(data.action_type or "")
    except ValueError:
        errors["action_type"] = "İşlem tipini seçin."
    steps = _steps(data.steps, errors)
    try:
        mode = StepMode(data.mode or "")
    except ValueError:
        errors["mode"] = "Adımların sırayla mı aynı anda mı işleyeceğini seçin."
        mode = StepMode.SEQUENTIAL
    try:
        behavior = RejectBehavior(data.reject_behavior or "")
    except ValueError:
        errors["reject_behavior"] = "Ret davranışını seçin."
        behavior = RejectBehavior.SINGLE_REJECTS
    try:
        basis = DayBasis(data.day_basis or "")
    except ValueError:
        errors["day_basis"] = "İş günü mü takvim günü mü, seçin."
        basis = DayBasis.CALENDAR
    if data.remind_after_days is None or data.escalate_after_days is None:
        errors["remind_after_days"] = "Hatırlatma ve eskalasyon gününü yazın."
    else:
        errors.update(check_timing(data.remind_after_days, data.escalate_after_days))
    escalation: SiteRole | None = None
    if data.escalation_role:
        try:
            escalation = SiteRole(data.escalation_role)
        except ValueError:
            errors["escalation_role"] = "Geçerli bir eskalasyon rolü seçin."
        else:
            if escalation not in APPROVER_ROLES or any(escalation in s.roles for s in steps):
                errors["escalation_role"] = (
                    "Eskalasyon hedefi adımların onaycı rolünden farklı olmalı."
                )
    if data.effective_from is None or data.effective_from < today:
        errors["effective_from"] = "Yürürlük tarihi bugün ya da ileri bir tarih olmalı."
    for name, value in (("amount_min", data.amount_min), ("amount_max", data.amount_max)):
        if value is not None and value < 0:
            errors[name] = "Tutar sıfırdan küçük olamaz."
    if (
        data.amount_min is not None
        and data.amount_max is not None
        and data.amount_max <= data.amount_min
    ):
        errors["amount_max"] = "Üst sınır alt sınırdan büyük olmalı."
    condition = Condition(data.amount_min, data.amount_max, (data.source or "").strip() or None)

    counts = await _holder_counts(session, site)
    for index, step in enumerate(steps, start=1):
        n = sum(counts.get(r, 0) for r in step.roles)
        if n == 0:
            errors.setdefault(f"steps.{index}", f"{index}. adımın rolünde aktif kullanıcı yok.")
        elif step.quorum.kind is QuorumKind.COUNT and (step.quorum.count or 0) > n:
            errors.setdefault(
                f"steps.{index}",
                f"{index}. adımda {n} onaycı var; {step.quorum.count} onay istenemez.",
            )
    warnings: list[str] = []
    if action is not None and "amount_min" not in errors and "amount_max" not in errors:
        others = await _active_conditions(
            session, action, exclude_rule_id=exclude_rule_id, today=today
        )
        if any(ranges_overlap(condition, other) for other in others):
            errors["amount_min"] = (
                "Bu tutar aralığı aynı işlem tipindeki başka bir kuralla çakışıyor."
            )
        else:
            for low, high in coverage_gaps([*others, condition]):
                warnings.append(_gap_text(low, high))
    if errors:
        raise FormFieldError(errors)
    if action is None or data.effective_from is None:  # pragma: no cover - yukarıda denetlendi
        raise FormFieldError(errors)
    return CheckedRule(
        action=action,
        condition=condition,
        steps=steps,
        mode=mode,
        reject_behavior=behavior,
        remind_after_days=data.remind_after_days or 0,
        escalate_after_days=data.escalate_after_days or 0,
        day_basis=basis,
        escalation_role=escalation,
        effective_from=data.effective_from,
        basis_note=(data.basis_note or "").strip()[:500] or None,
        warnings=warnings,
    )


def _gap_text(low: Decimal | None, high: Decimal | None) -> str:
    if low is None and high is not None:
        return f"{format_money_tr(high)} ve altı için kural yok; varsayılan kural uygulanır."
    if high is None and low is not None:
        return f"{format_money_tr(low)} üstü için kural yok; varsayılan kural uygulanır."
    return (
        f"{format_money_tr(low or Decimal(0))}–{format_money_tr(high or Decimal(0))} aralığı için "
        "kural yok; varsayılan kural uygulanır."
    )


def _version_condition(v: ApprovalRuleVersion) -> Condition:
    return Condition(v.amount_min, v.amount_max, v.source)


async def effective_version(
    session: AsyncSession, rule: ApprovalRule, today: date
) -> ApprovalRuleVersion | None:
    """Kuralın bugün yürürlükteki versiyonu (ileri tarihli versiyonlar beklemede)."""
    found: ApprovalRuleVersion | None = await session.scalar(
        select(ApprovalRuleVersion)
        .where(ApprovalRuleVersion.rule_id == rule.id, ApprovalRuleVersion.effective_from <= today)
        .order_by(ApprovalRuleVersion.version.desc())
        .limit(1)
    )
    return found


async def latest_version(session: AsyncSession, rule: ApprovalRule) -> ApprovalRuleVersion:
    found = await session.scalar(
        select(ApprovalRuleVersion).where(
            ApprovalRuleVersion.rule_id == rule.id,
            ApprovalRuleVersion.version == rule.current_version,
        )
    )
    if found is None:  # pragma: no cover - her kuralın en az bir versiyonu var
        raise RuntimeError("Kural versiyonu yok")
    return found


async def _active_conditions(
    session: AsyncSession, action: ActionType, *, exclude_rule_id: uuid.UUID | None, today: date
) -> list[Condition]:
    """Çakışma denetimi en son versiyona bakar (ileri tarihli olan da yürürlüğe girecek)."""
    rules = await session.scalars(
        select(ApprovalRule).where(ApprovalRule.action_type == action.value, ApprovalRule.is_active)
    )
    found: list[Condition] = []
    for rule in rules:
        if rule.id == exclude_rule_id:
            continue
        found.append(_version_condition(await latest_version(session, rule)))
    return found


async def match_rule(
    session: AsyncSession,
    action: ActionType,
    *,
    amount: Decimal | None,
    source: str | None,
    today: date,
) -> ApprovalRuleVersion | None:
    """Yürürlükteki kurallardan talebe uyanı (en dar koşullu); yoksa None → varsayılan kural."""
    best: ApprovalRuleVersion | None = None
    rules = await session.scalars(
        select(ApprovalRule).where(ApprovalRule.action_type == action.value, ApprovalRule.is_active)
    )
    for rule in rules:
        version = await effective_version(session, rule, today)
        if version is None or not _version_condition(version).matches(amount, source):
            continue
        if best is None or (
            _version_condition(version).specificity > _version_condition(best).specificity
        ):
            best = version
    return best


def _steps_json(steps: list[Step]) -> list[dict[str, Any]]:
    return [
        {
            "roles": [r.value for r in s.roles],
            "quorum": s.quorum.kind.value,
            "count": s.quorum.count,
        }
        for s in steps
    ]


def snapshot_of(version: ApprovalRuleVersion | None) -> dict[str, Any]:
    """Talebin donmuş kural kopyası. Varsayılan kural: tek adım, site yöneticisi, 1/1;
    hatırlatma/eskalasyon yok (kuralı olmayan işlem — süre tanımlanmamış)."""
    if version is None:
        return {
            "rule_id": None,
            "version": None,
            "mode": StepMode.SEQUENTIAL.value,
            "reject_behavior": RejectBehavior.SINGLE_REJECTS.value,
            "remind_after_days": None,
            "escalate_after_days": None,
            "day_basis": DayBasis.CALENDAR.value,
            "escalation_role": None,
            "steps": _steps_json(list(DEFAULT_STEPS)),
        }
    return {
        "rule_id": str(version.rule_id),
        "version": version.version,
        "mode": version.mode,
        "reject_behavior": version.reject_behavior,
        "remind_after_days": version.remind_after_days,
        "escalate_after_days": version.escalate_after_days,
        "day_basis": version.day_basis,
        "escalation_role": version.escalation_role,
        "steps": list(version.steps),
    }


def snapshot_steps(snapshot: dict[str, Any]) -> list[Step]:
    return [
        Step(
            tuple(SiteRole(r) for r in s["roles"]),
            Quorum(QuorumKind(s["quorum"]), s.get("count")),
        )
        for s in snapshot["steps"]
    ]


# --- Kural yazma (doğrudan ya da onay kuralı değişikliği onayıyla) ----------------------


async def _write_rule(
    session: AsyncSession,
    checked: CheckedRule,
    *,
    rule: ApprovalRule | None,
    created_by: str,
    template: bool = False,
) -> ApprovalRule:
    if rule is None:
        rule = ApprovalRule(id=uuid.uuid7(), action_type=checked.action.value, is_template=template)
        session.add(rule)
        version_no = 1
    else:
        version_no = rule.current_version + 1
        rule.current_version = version_no
        rule.is_template = False
    session.add(
        ApprovalRuleVersion(
            rule_id=rule.id,
            version=version_no,
            amount_min=checked.condition.amount_min,
            amount_max=checked.condition.amount_max,
            source=checked.condition.source,
            mode=checked.mode.value,
            reject_behavior=checked.reject_behavior.value,
            remind_after_days=checked.remind_after_days,
            escalate_after_days=checked.escalate_after_days,
            day_basis=checked.day_basis.value,
            escalation_role=checked.escalation_role.value if checked.escalation_role else None,
            effective_from=checked.effective_from,
            steps=_steps_json(checked.steps),
            basis_note=checked.basis_note,
            created_by_name=created_by,
        )
    )
    await session.flush()
    return rule


@dataclass(frozen=True, slots=True)
class RuleWriteResult:
    rule: ApprovalRule | None  # doğrudan yazıldıysa
    request: ApprovalRequest | None  # onaya gönderildiyse
    warnings: list[str]


def _rule_payload(op: str, rule_id: uuid.UUID | None, data: RuleData | None) -> dict[str, Any]:
    body: dict[str, Any] = {"op": op, "rule_id": str(rule_id) if rule_id else None}
    if data is not None:
        body["data"] = {
            "action_type": data.action_type,
            "steps": data.steps,
            "mode": data.mode,
            "reject_behavior": data.reject_behavior,
            "amount_min": str(data.amount_min) if data.amount_min is not None else None,
            "amount_max": str(data.amount_max) if data.amount_max is not None else None,
            "source": data.source,
            "remind_after_days": data.remind_after_days,
            "escalate_after_days": data.escalate_after_days,
            "day_basis": data.day_basis,
            "escalation_role": data.escalation_role,
            "effective_from": data.effective_from.isoformat() if data.effective_from else None,
            "basis_note": data.basis_note,
        }
    return body


def _rule_data(raw: dict[str, Any]) -> RuleData:
    return RuleData(
        action_type=raw.get("action_type"),
        steps=raw.get("steps"),
        mode=raw.get("mode"),
        reject_behavior=raw.get("reject_behavior"),
        amount_min=Decimal(raw["amount_min"]) if raw.get("amount_min") else None,
        amount_max=Decimal(raw["amount_max"]) if raw.get("amount_max") else None,
        source=raw.get("source"),
        remind_after_days=raw.get("remind_after_days"),
        escalate_after_days=raw.get("escalate_after_days"),
        day_basis=raw.get("day_basis"),
        escalation_role=raw.get("escalation_role"),
        effective_from=date.fromisoformat(raw["effective_from"])
        if raw.get("effective_from")
        else None,
        basis_note=raw.get("basis_note"),
    )


async def _change_needs_approval(session: AsyncSession, today: date) -> bool:
    """ONY-01 adım 4: "Onay kuralı değişikliği" için yürürlükte bir kural varsa değişiklik onaya
    gider; yoksa doğrudan yazılır (kuralsız işlemler için varsayılan kural burada uygulanmaz —
    aksi hâlde kuralı yazan yönetici kendi değişikliğini hiçbir zaman yürürlüğe sokamazdı)."""
    return (
        await match_rule(
            session, ActionType.APPROVAL_RULE_CHANGE, amount=None, source=None, today=today
        )
        is not None
    )


async def save_rule(
    session: AsyncSession,
    site: Site,
    data: RuleData,
    *,
    rule: ApprovalRule | None,
    actor: Person,
    today: date,
    now: datetime,
) -> RuleWriteResult:
    if rule is not None and data.action_type != rule.action_type:
        raise FormFieldError({"action_type": "Kuralın işlem tipi değiştirilemez; yeni kural açın."})
    checked = await check_rule(
        session, site, data, today=today, exclude_rule_id=rule.id if rule else None
    )
    if await _change_needs_approval(session, today):
        label = ACTION_LABELS[checked.action]
        request = await request_approval(
            session,
            site,
            NewRequest(
                action_type=ActionType.APPROVAL_RULE_CHANGE,
                title=f"Onay kuralı {'değişikliği' if rule else 'ekleme'} — {label}",
                payload=_rule_payload(
                    "update" if rule else "create", rule.id if rule else None, data
                ),
                summary={"action_type": checked.action.value, "warnings": checked.warnings},
                subject_type="approval_rules",
                subject_id=rule.id if rule else None,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        return RuleWriteResult(None, request, checked.warnings)
    written = await _write_rule(session, checked, rule=rule, created_by=actor.name)
    return RuleWriteResult(written, None, checked.warnings)


async def archive_rule(
    session: AsyncSession,
    site: Site,
    rule: ApprovalRule,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> RuleWriteResult:
    if not rule.is_active:
        raise ApprovalError(409, "rule_archived", "Kural zaten arşivde.")
    if await _change_needs_approval(session, today):
        request = await request_approval(
            session,
            site,
            NewRequest(
                action_type=ActionType.APPROVAL_RULE_CHANGE,
                title=f"Onay kuralı arşivleme — {ACTION_LABELS[ActionType(rule.action_type)]}",
                payload=_rule_payload("archive", rule.id, None),
                subject_type="approval_rules",
                subject_id=rule.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        return RuleWriteResult(None, request, [])
    rule.is_active = False
    await session.flush()
    return RuleWriteResult(rule, None, [])


async def _execute_rule_change(session: AsyncSession, request: ApprovalRequest) -> None:
    """Onaylanan kural değişikliğini uygular; o arada geçersiz olduysa yürütülemez."""
    payload = request.payload or {}
    scope = current_scope()
    site = await session.get(Site, scope.site_id) if scope and scope.site_id else None
    if site is None:  # pragma: no cover
        raise ExecutionError("Site bulunamadı.")
    rule = None
    if payload.get("rule_id"):
        rule = await session.get(ApprovalRule, uuid.UUID(payload["rule_id"]))
        if rule is None or not rule.is_active:
            raise ExecutionError("Kural bu arada arşivlendi.")
    if payload.get("op") == "archive":
        if rule is None:
            raise ExecutionError("Arşivlenecek kural belirtilmemiş.")
        rule.is_active = False
        await session.flush()
        return
    # Onay, önerilen yürürlük tarihinden sonra geldiyse kural onay günü yürürlüğe girer.
    decided = request.decided_at.astimezone(BUSINESS_TZ).date() if request.decided_at else None
    data = _rule_data(payload.get("data") or {})
    if decided is not None and (data.effective_from is None or data.effective_from < decided):
        data = replace(data, effective_from=decided)
    try:
        checked = await check_rule(
            session,
            site,
            data,
            today=decided or data.effective_from or date.min,
            exclude_rule_id=rule.id if rule else None,
        )
    except FormFieldError as exc:
        raise ExecutionError(
            "Kural artık geçerli değil: " + "; ".join(exc.fields.values())
        ) from exc
    await _write_rule(session, checked, rule=rule, created_by=request.requested_by_name)


register_executor(ActionType.APPROVAL_RULE_CHANGE, _execute_rule_change)


# --- Hazır şablonlar ("Önerilen") --------------------------------------------------------

# Yazılım Dokümanı 6.2'de adımları tanımlı şablonlar. Süreler dokümanın S3 örneğinden (X=2, Y=5
# gün); ret davranışı "tek ret reddeder"; eskalasyon hedefi yok (K20). Adımları tanımlanmamış
# tipler için şablon yok — varsayılan kural (site yöneticisi 1/1) uygulanır.
_ANY, _MAJ = QuorumKind.ANY.value, QuorumKind.MAJORITY.value
TEMPLATES: list[dict[str, Any]] = [
    {
        "action_type": ActionType.CHARGE_RULE,
        "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": _MAJ}],
    },
    {
        "action_type": ActionType.CHARGE_RUN,
        "steps": [
            {"roles": ["Muhasebe"], "quorum": _ANY},
            {"roles": ["Yönetici"], "quorum": _ANY},
        ],
    },
    {"action_type": ActionType.REFUND, "steps": [{"roles": ["Yönetici"], "quorum": _ANY}]},
    {
        "action_type": ActionType.PURCHASE_REQUEST,
        "amount_max": Decimal("10000.00"),
        "steps": [{"roles": ["Yönetici"], "quorum": _ANY}],
    },
    {
        "action_type": ActionType.PURCHASE_REQUEST,
        "amount_min": Decimal("10000.00"),
        "amount_max": Decimal("100000.00"),
        "steps": [
            {"roles": ["Yönetici"], "quorum": _ANY},
            {"roles": ["Yönetim Kurulu Üyesi"], "quorum": _ANY},
        ],
    },
    {
        "action_type": ActionType.PURCHASE_REQUEST,
        "amount_min": Decimal("100000.00"),
        "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": _MAJ}],
    },
    {"action_type": ActionType.REGISTRATION, "steps": [{"roles": ["Yönetici"], "quorum": _ANY}]},
]
TEMPLATE_REMIND, TEMPLATE_ESCALATE = 2, 5


@dataclass(frozen=True, slots=True)
class RestoreResult:
    created: list[ApprovalRule]
    skipped: list[str]  # Türkçe gerekçe


async def restore_defaults(
    session: AsyncSession, site: Site, *, actor: Person, today: date
) -> RestoreResult:
    """Kuralı olmayan işlem tiplerine şablonu kurar; mevcut kurala dokunmaz. Rolde kullanıcı
    yoksa (ör. sitede YK üyesi tanımlı değil) şablon kurulmaz, gerekçesi döner."""
    existing = {
        ActionType(a)
        for a in await session.scalars(
            select(ApprovalRule.action_type).where(ApprovalRule.is_active)
        )
    }
    created: list[ApprovalRule] = []
    skipped: list[str] = []
    for template in TEMPLATES:
        action = template["action_type"]
        if action in existing:
            continue
        data = RuleData(
            action_type=action.value,
            steps=template["steps"],
            amount_min=template.get("amount_min"),
            amount_max=template.get("amount_max"),
            remind_after_days=TEMPLATE_REMIND,
            escalate_after_days=TEMPLATE_ESCALATE,
            effective_from=today,
            basis_note="Önerilen hazır şablon",
        )
        try:
            checked = await check_rule(session, site, data, today=today)
        except FormFieldError as exc:
            skipped.append(f"{ACTION_LABELS[action]}: " + "; ".join(exc.fields.values()))
            continue
        created.append(
            await _write_rule(session, checked, rule=None, created_by=actor.name, template=True)
        )
    return RestoreResult(created, skipped)


# --- Talep --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewRequest:
    action_type: ActionType
    title: str
    summary: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] | None = None
    amount: Decimal | None = None
    source: str | None = None
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    post_approval: bool = False


async def _next_number(session: AsyncSession) -> int:
    scope = current_scope()
    await session.execute(
        _NUMBER_LOCK, {"key": f"approval_requests:{scope.site_id if scope else ''}"}
    )
    current = await session.scalar(select(func.max(ApprovalRequest.number)))
    return (current or 0) + 1


async def _role_holders(
    session: AsyncSession, site: Site, roles: tuple[SiteRole, ...]
) -> list[tuple[uuid.UUID, str, SiteRole]]:
    return [
        (m.user.id, m.user.full_name, m.role)
        for m in await members_svc.list_members(session, site)
        if m.is_active and m.user.is_active and m.role in roles
    ]


def _steps_to_open(request: ApprovalRequest) -> list[int]:
    count = len(request.snapshot["steps"])
    if request.snapshot["mode"] == StepMode.PARALLEL.value:
        return list(range(count))
    return [request.current_step]


async def _open_steps(
    session: AsyncSession, site: Site, request: ApprovalRequest, indexes: list[int], now: datetime
) -> None:
    """Adım(lar)ın onaycılarını çözer: talep eden çıkar; gereken onay M'yi kaydeder."""
    steps = snapshot_steps(request.snapshot)
    already: set[uuid.UUID] = set(
        await session.scalars(
            select(ApprovalDecision.user_id).where(
                ApprovalDecision.request_id == request.id,
                ApprovalDecision.revision == request.revision,
            )
        )
    )
    snapshot = dict(request.snapshot)
    snapshot_steps_json = [dict(s) for s in snapshot["steps"]]
    recipients: set[uuid.UUID] = set()
    for index in indexes:
        holders = [
            h
            for h in await _role_holders(session, site, steps[index].roles)
            if h[0] != request.requested_by and h[0] not in already
        ]
        for user_id, name, role in holders:
            session.add(
                ApprovalAssignment(
                    request_id=request.id,
                    revision=request.revision,
                    step_index=index,
                    user_id=user_id,
                    user_name=name,
                    role=role.value,
                )
            )
            recipients.add(user_id)
        snapshot_steps_json[index]["required"] = steps[index].quorum.required(len(holders))
        snapshot_steps_json[index]["eligible"] = len(holders)
    snapshot["steps"] = snapshot_steps_json
    request.snapshot = snapshot
    request.step_started_at = now
    request.reminded_at = None
    request.escalated_at = None
    await session.flush()
    await _notify(
        session,
        recipients,
        request,
        "approval.requested",
        f"Onayınız bekleniyor: {request.title}",
        key=f"open:{request.revision}:{','.join(map(str, indexes))}",
    )


async def _notify(
    session: AsyncSession,
    users: set[uuid.UUID] | list[uuid.UUID],
    request: ApprovalRequest,
    kind: str,
    title: str,
    *,
    key: str,
    body: str | None = None,
) -> None:
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=body or f"#{request.number} · {ACTION_LABELS[ActionType(request.action_type)]}",
            link_entity="approval_requests",
            link_id=request.id,
        ),
        dedupe_key=f"approval:{request.id}:{key}",
    )


async def request_approval(
    session: AsyncSession,
    site: Site,
    new: NewRequest,
    *,
    requester: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest:
    """Modülün çağırdığı tek giriş: kuralı seçer, talebi "onay bekliyor" olarak açar."""
    if new.action_type not in _EXECUTORS:
        raise ApprovalError(
            409, "action_not_supported", "Bu işlem tipi henüz onay mekanizmasına bağlı değil."
        )
    version = await match_rule(
        session, new.action_type, amount=new.amount, source=new.source, today=today
    )
    request = ApprovalRequest(
        id=uuid.uuid7(),
        number=await _next_number(session),
        action_type=new.action_type.value,
        subject_type=new.subject_type,
        subject_id=new.subject_id,
        title=" ".join(new.title.split())[:TITLE_MAX],
        summary=new.summary,
        payload=new.payload,
        amount=new.amount,
        source=new.source,
        post_approval=new.post_approval,
        requested_by=requester.user_id,
        requested_by_name=requester.name,
        rule_version_id=version.id if version else None,
        snapshot=snapshot_of(version),
        status=ApprovalStatus.PENDING.value,
        revision=1,
        current_step=0,
    )
    session.add(request)
    await session.flush()
    await _open_steps(session, site, request, _steps_to_open(request), now)
    return request


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, lock: bool = False
) -> ApprovalRequest | None:
    query = select(ApprovalRequest).where(ApprovalRequest.id == request_id)
    found: ApprovalRequest | None = await session.scalar(query.with_for_update() if lock else query)
    return found


# --- Yetki devri --------------------------------------------------------------------------


def _active_delegation(now: datetime) -> list[Any]:
    return [
        ApprovalDelegation.starts_at <= now,
        ApprovalDelegation.ends_at > now,
        ApprovalDelegation.ended_at.is_(None),
    ]


async def delegators_for(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> list[ApprovalDelegation]:
    """Bu kişiye şu an etkin devredilmiş yetkiler."""
    return list(
        await session.scalars(
            select(ApprovalDelegation).where(
                ApprovalDelegation.to_user_id == user_id, *_active_delegation(now)
            )
        )
    )


async def create_delegation(
    session: AsyncSession,
    site: Site,
    *,
    delegator: Person,
    to_user_id: uuid.UUID | None,
    roles: list[str] | None,
    starts_at: datetime | None,
    ends_at: datetime | None,
    note: str | None,
    now: datetime,
) -> ApprovalDelegation:
    errors: dict[str, str] = {}
    members = {
        m.user.id: m
        for m in await members_svc.list_members(session, site)
        if m.is_active and m.user.is_active
    }
    own = members.get(delegator.user_id)
    try:
        chosen = {SiteRole(r) for r in roles or []}
    except ValueError:
        chosen = set()
    if not chosen:
        errors["roles"] = "Devredilecek rolü seçin."
    elif own is None or chosen != {own.role}:
        errors["roles"] = "Yalnız kendi rolünüzü devredebilirsiniz."
    target = members.get(to_user_id) if to_user_id else None
    if target is None or to_user_id == delegator.user_id:
        errors["to_user_id"] = "Devralacak kişiyi seçin."
    elif own is not None and target.role != own.role:
        # K20: rol sırası tanımlanana kadar yalnız aynı rol (ONY-03 S1)
        errors["to_user_id"] = "Devralan aynı role sahip olmalı."
    if starts_at is None or ends_at is None:
        errors["starts_at"] = "Başlangıç ve bitiş zamanını seçin."
    elif ends_at <= starts_at:
        errors["ends_at"] = "Bitiş başlangıçtan sonra olmalı."
    elif ends_at <= now:
        errors["ends_at"] = "Bitiş ileri bir zaman olmalı."
    if errors:
        raise FormFieldError(errors)
    if starts_at is None or ends_at is None or target is None:  # pragma: no cover
        raise FormFieldError(errors)
    overlapping = await session.scalar(
        select(ApprovalDelegation.id).where(
            ApprovalDelegation.from_user_id == delegator.user_id,
            ApprovalDelegation.ended_at.is_(None),
            ApprovalDelegation.ends_at > starts_at,
            ApprovalDelegation.starts_at < ends_at,
            ApprovalDelegation.roles.overlap([r.value for r in chosen]),
        )
    )
    if overlapping is not None:
        raise ApprovalError(
            409, "delegation_overlaps", "Bu rol için bu aralıkta başka bir devir var."
        )
    delegation = ApprovalDelegation(
        id=uuid.uuid7(),
        from_user_id=delegator.user_id,
        from_name=delegator.name,
        to_user_id=target.user.id,
        to_name=target.user.full_name,
        roles=sorted(r.value for r in chosen),
        starts_at=starts_at,
        ends_at=ends_at,
        note=" ".join((note or "").split())[:500] or None,
    )
    session.add(delegation)
    await session.flush()
    if starts_at <= now:
        await _delegation_started(session, delegation)
    return delegation


async def _delegation_started(session: AsyncSession, d: ApprovalDelegation) -> None:
    d.start_notified = True
    await notify_svc.notify(
        session,
        [d.to_user_id],
        notify_svc.Message(
            NotificationCategory.OPERATIONS,
            "delegation.started",
            f"{d.from_name} onay yetkisini size devretti",
            f"{d.ends_at:%d.%m.%Y %H:%M} tarihine kadar vekâleten karar verebilirsiniz.",
            link_entity="approval_delegations",
            link_id=d.id,
        ),
        dedupe_key=f"delegation:{d.id}:start",
    )


async def end_delegation(
    session: AsyncSession, delegation: ApprovalDelegation, *, user_id: uuid.UUID, now: datetime
) -> ApprovalDelegation:
    if delegation.from_user_id != user_id:
        raise ApprovalError(404, "not_found", "Devir bulunamadı.")
    if delegation.ended_at is not None or delegation.ends_at <= now:
        raise ApprovalError(409, "delegation_ended", "Devir zaten sona erdi.")
    delegation.ended_at = now
    await session.flush()
    return delegation


# --- Karar ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Slot:
    assignment: ApprovalAssignment
    delegation: ApprovalDelegation | None  # vekâleten ise


async def open_assignments(
    session: AsyncSession, request: ApprovalRequest
) -> list[ApprovalAssignment]:
    return list(
        await session.scalars(
            select(ApprovalAssignment)
            .where(
                ApprovalAssignment.request_id == request.id,
                ApprovalAssignment.revision == request.revision,
                ApprovalAssignment.step_index.in_(_open_indexes(request)),
            )
            .order_by(ApprovalAssignment.step_index, ApprovalAssignment.user_name)
        )
    )


def _open_indexes(request: ApprovalRequest) -> list[int]:
    if ApprovalStatus(request.status) not in OPEN_STATUSES:
        return []
    steps = request.snapshot["steps"]
    if request.snapshot["mode"] == StepMode.PARALLEL.value:
        return [i for i, s in enumerate(steps) if s.get("outcome") is None]
    return [request.current_step]


async def find_slot(
    session: AsyncSession, request: ApprovalRequest, user_id: uuid.UUID, now: datetime
) -> Slot | None:
    """Kişinin bu talepte kullanabileceği karar hakkı: önce kendi, sonra vekâleten."""
    if ApprovalStatus(request.status) is not ApprovalStatus.PENDING:
        return None
    decided = {
        (d.user_id, d.slot_user_id)
        for d in await session.execute(
            select(ApprovalDecision.user_id, ApprovalDecision.slot_user_id).where(
                ApprovalDecision.request_id == request.id,
                ApprovalDecision.revision == request.revision,
            )
        )
    }
    deciders = {u for u, _ in decided}
    used_slots = {s for _, s in decided}
    if user_id in deciders:
        return None
    assignments = [
        a for a in await open_assignments(session, request) if a.user_id not in used_slots
    ]
    for a in assignments:
        if a.user_id == user_id:
            return Slot(a, None)
    for d in await delegators_for(session, user_id, now):
        if d.from_user_id == request.requested_by:
            continue  # devralan, devredenin kendi talebini onaylayamaz
        for a in assignments:
            if a.user_id == d.from_user_id and a.role in d.roles:
                return Slot(a, d)
    return None


@contextmanager
def _as_delegate(delegation: ApprovalDelegation | None) -> Iterator[None]:
    """Vekâleten verilen kararın denetim satırı `delegate` aktör tipiyle yazılır."""
    if delegation is None:
        yield
        return
    actor = current_actor()
    if actor is None:
        yield
        return
    set_actor(Actor(actor.user_id, actor.name, actor.ip, actor.user_agent, ActorType.DELEGATE))
    try:
        yield
    finally:
        set_actor(actor)


async def decide(
    session: AsyncSession,
    site: Site,
    request: ApprovalRequest,
    *,
    decider: Person,
    decision: Decision,
    comment: str | None,
    now: datetime,
) -> ApprovalRequest:
    """`request` kilitli gelmeli (`get_request(lock=True)`)."""
    if ApprovalStatus(request.status) is not ApprovalStatus.PENDING:
        raise ApprovalError(409, "not_pending", "Bu talep artık karar beklemiyor.")
    if decider.user_id == request.requested_by:
        raise ApprovalError(403, "own_request", "Kendi talebinizi onaylayamazsınız.")
    text_ = " ".join((comment or "").split())[:COMMENT_MAX] or None
    if decision is not Decision.APPROVE and text_ is None:
        raise FormFieldError({"comment": "Gerekçe yazın."})
    slot = await find_slot(session, request, decider.user_id, now)
    if slot is None:
        raise ApprovalError(403, "not_an_approver", "Bu talepte karar verme hakkınız yok.")
    # Karar anında yetki hâlâ geçerli mi: asıl onaycı rolünü koruyor mu (devirde devralan da)?
    holders = {h[0] for h in await _role_holders(session, site, (SiteRole(slot.assignment.role),))}
    if slot.assignment.user_id not in holders or decider.user_id not in holders:
        raise ApprovalError(403, "role_changed", "Bu sitedeki yetkiniz değişti.")
    with _as_delegate(slot.delegation):
        session.add(
            ApprovalDecision(
                request_id=request.id,
                revision=request.revision,
                step_index=slot.assignment.step_index,
                user_id=decider.user_id,
                user_name=decider.name,
                slot_user_id=slot.assignment.user_id,
                on_behalf_of_name=slot.assignment.user_name if slot.delegation else None,
                decision=decision.value,
                comment=text_,
            )
        )
        await session.flush()
    if decision is Decision.REVISION:
        request.status = ApprovalStatus.REVISION_REQUESTED.value
        await session.flush()
        await _notify(
            session, [request.requested_by], request, "approval.revision_requested",
            f"Revizyon istendi: {request.title}", key=f"revision:{request.revision}", body=text_,
        )  # fmt: skip
        return request
    await _advance(session, site, request, slot.assignment.step_index, now)
    return request


async def _step_counts(
    session: AsyncSession, request: ApprovalRequest, step_index: int
) -> tuple[int, int]:
    rows = await session.execute(
        select(ApprovalDecision.decision, func.count())
        .where(
            ApprovalDecision.request_id == request.id,
            ApprovalDecision.revision == request.revision,
            ApprovalDecision.step_index == step_index,
        )
        .group_by(ApprovalDecision.decision)
    )
    counts: dict[str, int] = {str(d): int(c) for d, c in rows.all()}
    return counts.get(Decision.APPROVE.value, 0), counts.get(Decision.REJECT.value, 0)


async def _advance(
    session: AsyncSession, site: Site, request: ApprovalRequest, step_index: int, now: datetime
) -> None:
    approvals, rejections = await _step_counts(session, request, step_index)
    step = request.snapshot["steps"][step_index]
    outcome = step_outcome(
        approvals=approvals,
        rejections=rejections,
        required=step.get("required", 1),
        behavior=RejectBehavior(request.snapshot["reject_behavior"]),
    )
    if outcome is StepOutcome.PENDING:
        return
    snapshot = dict(request.snapshot)
    steps = [dict(s) for s in snapshot["steps"]]
    steps[step_index]["outcome"] = outcome.value
    snapshot["steps"] = steps
    request.snapshot = snapshot
    if outcome is StepOutcome.REJECTED:
        request.status = ApprovalStatus.REJECTED.value
        request.decided_at = now
        await session.flush()
        await _notify(
            session, [request.requested_by], request, "approval.rejected",
            f"Reddedildi: {request.title}", key=f"rejected:{request.revision}",
        )  # fmt: skip
        return
    if request.snapshot["mode"] == StepMode.PARALLEL.value:
        if any(s.get("outcome") != StepOutcome.APPROVED.value for s in steps):
            await session.flush()
            return
    elif step_index + 1 < len(steps):
        request.current_step = step_index + 1
        await session.flush()
        await _open_steps(session, site, request, [request.current_step], now)
        return
    await _finish(session, request, now)


async def _finish(session: AsyncSession, request: ApprovalRequest, now: datetime) -> None:
    request.status = ApprovalStatus.APPROVED.value
    request.decided_at = now
    await session.flush()
    executor = _EXECUTORS[ActionType(request.action_type)]
    try:
        async with session.begin_nested():
            await executor(session, request)
    except ExecutionError as exc:
        request.status = ApprovalStatus.EXECUTION_FAILED.value
        request.failure_reason = str(exc)[:COMMENT_MAX]
        await session.flush()
        await _notify(
            session, [request.requested_by], request, "approval.execution_failed",
            f"Onaylandı – yürütülemedi: {request.title}", key="failed", body=request.failure_reason,
        )  # fmt: skip
        return
    request.status = ApprovalStatus.EXECUTED.value
    await session.flush()
    await _notify(
        session, [request.requested_by], request, "approval.approved",
        f"Onaylandı: {request.title}", key="approved",
    )  # fmt: skip


async def withdraw(
    session: AsyncSession, request: ApprovalRequest, *, user_id: uuid.UUID, now: datetime
) -> ApprovalRequest:
    if request.requested_by != user_id:
        raise ApprovalError(403, "not_requester", "Yalnız talebi açan geri çekebilir.")
    if ApprovalStatus(request.status) not in OPEN_STATUSES:
        raise ApprovalError(409, "not_pending", "Bu talep artık geri çekilemez.")
    request.status = ApprovalStatus.WITHDRAWN.value
    request.decided_at = now
    await session.flush()
    return request


async def resubmit(
    session: AsyncSession,
    site: Site,
    request: ApprovalRequest,
    *,
    user_id: uuid.UUID,
    title: str | None,
    summary: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    amount: Decimal | None,
    now: datetime,
) -> ApprovalRequest:
    """Revizyon sonrası yeniden gönderim: yeni sürüm, zincir baştan; kural versiyonu aynı kalır."""
    if request.requested_by != user_id:
        raise ApprovalError(403, "not_requester", "Yalnız talebi açan yeniden gönderebilir.")
    if ApprovalStatus(request.status) is not ApprovalStatus.REVISION_REQUESTED:
        raise ApprovalError(409, "not_in_revision", "Bu talepte revizyon istenmedi.")
    if title:
        request.title = " ".join(title.split())[:TITLE_MAX]
    if summary is not None:
        request.summary = summary
    if payload is not None:
        request.payload = payload
    if amount is not None:
        request.amount = amount
    request.revision += 1
    request.status = ApprovalStatus.PENDING.value
    request.current_step = 0
    snapshot = dict(request.snapshot)
    snapshot["steps"] = [
        {k: v for k, v in s.items() if k not in {"outcome", "required", "eligible"}}
        for s in snapshot["steps"]
    ]
    request.snapshot = snapshot
    await session.flush()
    await _open_steps(session, site, request, _steps_to_open(request), now)
    return request


# --- Listeler ------------------------------------------------------------------------------


def pending_for(user_id: uuid.UUID, delegator_ids: list[uuid.UUID]) -> Select[ApprovalRequest]:
    """Benim (ya da vekâleten) kararımı bekleyenler: açık adımda kullanılmamış hakkım var."""
    holders = [user_id, *delegator_ids]
    # Alt sorgular ana talebe açıkça bağlanır (correlate): site kapsamı süzgeci alt sorguya
    # tabloyu yeniden ekleyince kendiliğinden bağlanma kopuyor ve başka talepteki karar sayılıyordu.
    slot_used = (
        select(ApprovalDecision.id)
        .where(
            ApprovalDecision.request_id == ApprovalRequest.id,
            ApprovalDecision.revision == ApprovalRequest.revision,
            ApprovalDecision.slot_user_id == ApprovalAssignment.user_id,
        )
        .correlate(ApprovalRequest, ApprovalAssignment)
        .exists()
    )
    open_slot = (
        select(ApprovalAssignment.id)
        .where(
            ApprovalAssignment.request_id == ApprovalRequest.id,
            ApprovalAssignment.revision == ApprovalRequest.revision,
            ApprovalAssignment.user_id.in_(holders),
            ~slot_used,
        )
        .correlate(ApprovalRequest)
        .exists()
    )
    already = (
        select(ApprovalDecision.id)
        .where(
            ApprovalDecision.request_id == ApprovalRequest.id,
            ApprovalDecision.revision == ApprovalRequest.revision,
            ApprovalDecision.user_id == user_id,
        )
        .correlate(ApprovalRequest)
        .exists()
    )
    return (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING.value, open_slot, ~already)
        .order_by(
            ApprovalRequest.post_approval.desc(),  # "sonradan onay" en üstte
            ApprovalRequest.created_at,
            ApprovalRequest.id,
        )
    )


def mine(user_id: uuid.UUID) -> Select[ApprovalRequest]:
    return (
        select(ApprovalRequest)
        .where(ApprovalRequest.requested_by == user_id)
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
    )


def history_for(user_id: uuid.UUID) -> Select[ApprovalRequest]:
    """Sonuçlanmış ve benim karar verdiğim ya da onaycısı olduğum talepler."""
    involved = or_(
        select(ApprovalAssignment.id)
        .where(
            ApprovalAssignment.request_id == ApprovalRequest.id,
            ApprovalAssignment.user_id == user_id,
        )
        .correlate(ApprovalRequest)
        .exists(),
        select(ApprovalDecision.id)
        .where(
            ApprovalDecision.request_id == ApprovalRequest.id,
            ApprovalDecision.user_id == user_id,
        )
        .correlate(ApprovalRequest)
        .exists(),
    )
    return (
        select(ApprovalRequest)
        .where(ApprovalRequest.status.not_in([s.value for s in OPEN_STATUSES]), involved)
        .order_by(ApprovalRequest.decided_at.desc().nulls_last(), ApprovalRequest.id.desc())
    )


async def can_view(
    session: AsyncSession,
    request: ApprovalRequest,
    user_id: uuid.UUID,
    *,
    oversight: bool,
    now: datetime,
) -> bool:
    """Talep eden, onaycı, karar veren, onaycının etkin vekili ya da kuralları yöneten görür."""
    if oversight or request.requested_by == user_id:
        return True
    delegators = [d.from_user_id for d in await delegators_for(session, user_id, now)]
    found = await session.scalar(
        select(ApprovalAssignment.id)
        .where(
            ApprovalAssignment.request_id == request.id,
            ApprovalAssignment.user_id.in_([user_id, *delegators]),
        )
        .limit(1)
    )
    if found is not None:
        return True
    decided = await session.scalar(
        select(ApprovalDecision.id)
        .where(ApprovalDecision.request_id == request.id, ApprovalDecision.user_id == user_id)
        .limit(1)
    )
    return decided is not None


# --- Zamanlanmış iş: hatırlatma, eskalasyon, devir başlangıcı (saatlik) ----------------


@dataclass(slots=True)
class TimerResult:
    reminded: int = 0
    escalated: int = 0
    delegations_started: int = 0


def due_dates(request: ApprovalRequest) -> tuple[date | None, date | None]:
    snap = request.snapshot
    if request.step_started_at is None or snap.get("remind_after_days") is None:
        return None, None
    basis = DayBasis(snap["day_basis"])
    start = request.step_started_at.date()
    return (
        add_days(start, snap["remind_after_days"], basis),
        add_days(start, snap["escalate_after_days"], basis),
    )


async def run_timers(
    session: AsyncSession, site: Site, *, now: datetime, today: date
) -> TimerResult:
    """Tekrar güvenli: hatırlatma ve eskalasyon adım başına bir kez (zaman damgası)."""
    result = TimerResult()
    pending = list(
        await session.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.status == ApprovalStatus.PENDING.value)
            .with_for_update(skip_locked=True)
        )
    )
    for request in pending:
        remind_on, escalate_on = due_dates(request)
        if remind_on is None or escalate_on is None:
            continue
        undecided = await _undecided(session, request)
        if request.reminded_at is None and today >= remind_on and undecided:
            request.reminded_at = now
            await _notify(
                session, undecided, request, "approval.reminder",
                f"Hatırlatma: onayınız bekleniyor — {request.title}",
                key=f"remind:{request.revision}:{request.current_step}",
            )  # fmt: skip
            result.reminded += 1
        if request.escalated_at is None and today >= escalate_on:
            request.escalated_at = now
            await _escalate(session, site, request)
            result.escalated += 1
    starting = await session.scalars(
        select(ApprovalDelegation).where(
            ApprovalDelegation.start_notified.is_(False), *_active_delegation(now)
        )
    )
    for delegation in starting:
        await _delegation_started(session, delegation)
        result.delegations_started += 1
    await session.flush()
    return result


async def _undecided(session: AsyncSession, request: ApprovalRequest) -> list[uuid.UUID]:
    used = set(
        await session.scalars(
            select(ApprovalDecision.slot_user_id).where(
                ApprovalDecision.request_id == request.id,
                ApprovalDecision.revision == request.revision,
            )
        )
    )
    return sorted(
        {a.user_id for a in await open_assignments(session, request) if a.user_id not in used}
    )


async def _escalate(session: AsyncSession, site: Site, request: ApprovalRequest) -> None:
    """Y gün dolunca: eskalasyon hedefi rolü de karar verebilir (asıl onaycının hakkı sürer).
    Hedef seçilmemişse (K20) yalnız etiket konur — hatırlatma zaten gitti."""
    role = request.snapshot.get("escalation_role")
    if not role:
        return
    existing = {
        (a.step_index, a.user_id)
        for a in await session.scalars(
            select(ApprovalAssignment).where(
                ApprovalAssignment.request_id == request.id,
                ApprovalAssignment.revision == request.revision,
            )
        )
    }
    added: set[uuid.UUID] = set()
    for index in _open_indexes(request):
        for user_id, name, holder_role in await _role_holders(session, site, (SiteRole(role),)):
            if user_id == request.requested_by or (index, user_id) in existing:
                continue
            session.add(
                ApprovalAssignment(
                    request_id=request.id,
                    revision=request.revision,
                    step_index=index,
                    user_id=user_id,
                    user_name=name,
                    role=holder_role.value,
                    source="escalation",
                )
            )
            added.add(user_id)
    await session.flush()
    await _notify(
        session, added, request, "approval.escalated",
        f"Eskalasyon: süresinde karar verilmeyen talep — {request.title}",
        key=f"escalate:{request.revision}:{request.current_step}",
    )  # fmt: skip


JOB_ACTOR = "Onay zamanlayıcısı"


async def run_all_timers(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, today: date
) -> list[tuple[uuid.UUID, TimerResult]]:
    """Saatlik iş (Yazılım Dokümanı 4.6): bekleyen talebi ya da başlayacak devri olan her site,
    site başına ayrı transaction — bir sitedeki hata diğerini durdurmaz."""
    with all_sites_scope():
        async with factory() as session:
            pending = select(ApprovalRequest.site_id).where(
                ApprovalRequest.status == ApprovalStatus.PENDING.value
            )
            starting = select(ApprovalDelegation.site_id).where(
                ApprovalDelegation.start_notified.is_(False), *_active_delegation(now)
            )
            site_ids = sorted(set(await session.scalars(pending.union(starting))))
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, TimerResult]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append((site_id, await run_timers(session, site, now=now, today=today)))
    return results
