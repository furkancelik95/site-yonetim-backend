"""Aidat kuralları (FIN-02; PM K23) — versiyonlu, deneme hesabı, "Aidat kuralı" onayı.

Site kapsamında.

Akış: kural + ilk sürüm taslak → deneme hesabı → onaya gönder. "Aidat kuralı" onay kuralı varsa
sürüm onay bekler (202), onaylanınca yürürlüğe girer; kural yoksa doğrudan yürürlüğe girer.
Yürürlük dönemi kilitli olamaz, geçmiş olamaz. Gönderimde bölüm verisi eksikse (m², arsa payı,
tip) 422 — eksik bölümler listelenir; aynı kalem tipinde çakışan kapsam 409.

Tahakkuk motoru, sitenin kaynağı `billing_rule` ise bir dönemde her kuralın o dönemde geçerli
sürümünü (`items_for`) kullanır: yürürlüğü o döneme kadar başlamış en son **aktif** sürüm.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.billing_rules import (
    VersionFields,
    check_name,
    check_version,
    is_due,
    periods_per_year,
    scopes_overlap,
    to_item,
)
from site_yonetim.domain.charging.engine import (
    ChargeInput,
    ComponentData,
    Exemption,
    ItemData,
    Simulation,
    WarningKind,
    simulate,
)
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    FinanceRuleError,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.fiscal import period_label
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import (
    BillingRule,
    BillingRuleVersion,
    Block,
    ChargeType,
    Document,
    Site,
    Unit,
    UnitType,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import period_lock
from site_yonetim.services.approvals import ExecutionError, Person

DRAFT, PENDING, ACTIVE, REJECTED = "draft", "approval_pending", "active", "rejected"
OPEN = (DRAFT, PENDING)


# --- Sürüm alanları ↔ satır --------------------------------------------------------------


def fields_of(v: BillingRuleVersion) -> VersionFields:
    return VersionFields(
        effective=YearMonth(v.effective_year, v.effective_month),
        kind=AllocationKind(v.kind),
        amount=v.amount,
        basis_note=v.basis_note,
        area_basis=AreaBasis(v.area_basis),
        frequency=Frequency(v.frequency),
        components=tuple(
            ComponentData(
                AllocationKind(c["kind"]),
                Decimal(str(c["percent"])),
                AreaBasis(c.get("area_basis", "gross")),
            )
            for c in v.components or []
        ),
        type_coefficients={
            uuid.UUID(k): Decimal(str(val)) for k, val in (v.type_coefficients or {}).items()
        },
        exemptions={
            uuid.UUID(e["unit_id"]): Exemption(Decimal(str(e["percent"])), e["reason"])
            for e in v.exemptions or []
        },
        scope_kind=ScopeKind(v.scope_kind),
        scope_block_ids=frozenset(v.scope_block_ids or ()),
        scope_unit_type_ids=frozenset(v.scope_unit_type_ids or ()),
        scope_usage=v.scope_usage,
        discontinued=v.discontinued,
    )


def _write(row: BillingRuleVersion, f: VersionFields, document_id: uuid.UUID | None) -> None:
    row.effective_year, row.effective_month = f.effective.year, f.effective.month
    row.kind = f.kind.value
    row.area_basis = f.area_basis.value
    row.amount_mode = f.amount_mode
    row.amount = f.amount
    row.frequency = f.frequency.value
    row.components = [
        {"kind": c.kind.value, "percent": str(c.percent), "area_basis": c.area_basis.value}
        for c in f.components
    ]
    row.type_coefficients = {str(k): str(v) for k, v in f.type_coefficients.items()}
    row.exemptions = [
        {"unit_id": str(k), "percent": str(e.percent), "reason": " ".join(e.reason.split())}
        for k, e in f.exemptions.items()
    ]
    row.scope_kind = f.scope_kind.value
    row.scope_block_ids = sorted(f.scope_block_ids) or None
    row.scope_unit_type_ids = sorted(f.scope_unit_type_ids) or None
    row.scope_usage = f.scope_usage
    row.discontinued = f.discontinued
    row.basis_note = " ".join(f.basis_note.split())
    row.basis_document_id = document_id


async def _check_refs(
    session: AsyncSession, f: VersionFields, document_id: uuid.UUID | None
) -> dict[str, str]:
    """Kimlikler bu sitenin kaydı mı (blok, tip, bölüm, belge)? RLS başka siteyi zaten gizler."""
    errors: dict[str, str] = {}

    async def missing(
        model: type[Block] | type[UnitType] | type[Unit], ids: set[uuid.UUID]
    ) -> bool:
        if not ids:
            return False
        found = await session.scalar(select(func.count()).where(model.id.in_(ids)))
        return (found or 0) != len(ids)

    if await missing(Block, set(f.scope_block_ids)):
        errors["scope_block_ids"] = "Seçilen bloklardan biri bulunamadı."
    if await missing(UnitType, set(f.scope_unit_type_ids) | set(f.type_coefficients)):
        errors["type_coefficients" if f.type_coefficients else "scope_unit_type_ids"] = (
            "Seçilen bölüm tiplerinden biri bulunamadı."
        )
    if await missing(Unit, set(f.exemptions)):
        errors["exemptions"] = "Muafiyetteki bölümlerden biri bulunamadı."
    if document_id is not None and await session.get(Document, document_id) is None:
        errors["basis_document_id"] = "Dayanak belge bulunamadı."
    return errors


async def _validated(
    session: AsyncSession, f: VersionFields, document_id: uuid.UUID | None
) -> None:
    errors = check_version(f) or await _check_refs(session, f, document_id)
    if errors:
        raise FormFieldError(errors)


# --- Okuma -----------------------------------------------------------------------------


async def get_rule(session: AsyncSession, rule_id: uuid.UUID) -> BillingRule | None:
    rule: BillingRule | None = await session.scalar(
        select(BillingRule).where(BillingRule.id == rule_id)
    )
    return rule


async def versions_of(
    session: AsyncSession, rule_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[BillingRuleVersion]]:
    out: dict[uuid.UUID, list[BillingRuleVersion]] = {r: [] for r in rule_ids}
    if rule_ids:
        for v in await session.scalars(
            select(BillingRuleVersion)
            .where(BillingRuleVersion.rule_id.in_(rule_ids))
            .order_by(BillingRuleVersion.version_no.desc())
        ):
            out[v.rule_id].append(v)
    return out


def effective_version(
    versions: list[BillingRuleVersion], period: YearMonth
) -> BillingRuleVersion | None:
    """Dönemde geçerli sürüm: yürürlüğü o döneme kadar başlamış en son aktif sürüm."""
    candidates = [
        v
        for v in versions
        if v.status == ACTIVE and YearMonth(v.effective_year, v.effective_month) <= period
    ]
    candidates.sort(key=lambda v: (v.effective_year, v.effective_month, v.version_no))
    return candidates[-1] if candidates else None


async def list_rules(session: AsyncSession) -> list[BillingRule]:
    return list(
        await session.scalars(
            select(BillingRule).order_by(BillingRule.sort_order, BillingRule.name)
        )
    )


async def items_for(session: AsyncSession, period: YearMonth) -> tuple[list[ItemData], list[str]]:
    """Tahakkuk girdisi (K23): dönemde geçerli sürümler; takvim gereği kesilmeyenlerin adları."""
    rules = await list_rules(session)
    versions = await versions_of(session, [r.id for r in rules])
    payers = {
        t.id: PayerRule(t.payer_rule)
        for t in await session.scalars(
            select(ChargeType).where(ChargeType.id.in_({r.charge_type_id for r in rules}))
        )
    }
    items: list[ItemData] = []
    not_due: list[str] = []
    for rule in rules:
        version = effective_version(versions[rule.id], period)
        if version is None or version.discontinued:
            continue
        f = fields_of(version)
        if not is_due(f.frequency, period):
            not_due.append(rule.name)
            continue
        items.append(
            to_item(
                version.id,
                rule.name,
                f,
                payer_rule=payers.get(rule.charge_type_id, PayerRule.OCCUPANT),
                owner_item=rule.owner_item,
            )
        )
    return items, not_due


# --- Yazma -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleInput:
    name: str
    charge_type_id: uuid.UUID
    owner_item: bool


async def _check_rule(session: AsyncSession, data: RuleInput) -> dict[str, str]:
    errors: dict[str, str] = {}
    if (message := check_name(data.name)) is not None:
        errors["name"] = message
    if await session.get(ChargeType, data.charge_type_id) is None:
        errors["charge_type_id"] = "Kalem tipi bulunamadı."
    return errors


async def create(
    session: AsyncSession,
    data: RuleInput,
    fields: VersionFields,
    *,
    document_id: uuid.UUID | None,
    actor: Person,
) -> tuple[BillingRule, BillingRuleVersion]:
    errors = await _check_rule(session, data) | check_version(fields)
    if errors:
        raise FormFieldError(errors)
    await _validated(session, fields, document_id)
    order = await session.scalar(select(func.max(BillingRule.sort_order))) or 0
    rule = BillingRule(
        id=uuid.uuid7(),
        name=" ".join(data.name.split()),
        charge_type_id=data.charge_type_id,
        owner_item=data.owner_item,
        sort_order=order + 1,
        created_by=actor.name,
    )
    session.add(rule)
    await session.flush()
    version = await _new_version(session, rule, fields, document_id, actor)
    return rule, version


async def _new_version(
    session: AsyncSession,
    rule: BillingRule,
    fields: VersionFields,
    document_id: uuid.UUID | None,
    actor: Person,
) -> BillingRuleVersion:
    last = await session.scalar(
        select(func.max(BillingRuleVersion.version_no)).where(BillingRuleVersion.rule_id == rule.id)
    )
    version = BillingRuleVersion(
        id=uuid.uuid7(),
        rule_id=rule.id,
        version_no=(last or 0) + 1,
        status=DRAFT,
        created_by_id=actor.user_id,
        created_by=actor.name,
    )
    _write(version, fields, document_id)
    session.add(version)
    await session.flush()
    return version


async def update_rule(session: AsyncSession, rule: BillingRule, data: RuleInput) -> None:
    """Ad, kalem tipi ve malik kalemi. Yürürlükte sürümü olan kuralın kalem tipi değişmez."""
    errors = await _check_rule(session, data)
    if errors:
        raise FormFieldError(errors)
    if data.charge_type_id != rule.charge_type_id or data.owner_item != rule.owner_item:
        active = await session.scalar(
            select(func.count()).where(
                BillingRuleVersion.rule_id == rule.id, BillingRuleVersion.status == ACTIVE
            )
        )
        if active:
            raise FinanceRuleError(
                "rule_in_use",
                "Yürürlüğe girmiş kuralın kalem tipi ve malik kalemi işareti değiştirilemez; "
                "yeni kural tanımlayın.",
                conflict=True,
            )
    rule.name = " ".join(data.name.split())
    rule.charge_type_id = data.charge_type_id
    rule.owner_item = data.owner_item
    await session.flush()


async def add_version(
    session: AsyncSession,
    rule: BillingRule,
    fields: VersionFields,
    *,
    document_id: uuid.UUID | None,
    actor: Person,
) -> BillingRuleVersion:
    open_count = await session.scalar(
        select(func.count()).where(
            BillingRuleVersion.rule_id == rule.id, BillingRuleVersion.status.in_(OPEN)
        )
    )
    if open_count:
        raise FinanceRuleError(
            "open_version_exists",
            "Bu kuralın taslak ya da onay bekleyen bir sürümü var; önce onu sonuçlandırın.",
            conflict=True,
        )
    await _validated(session, fields, document_id)
    return await _new_version(session, rule, fields, document_id, actor)


async def update_version(
    session: AsyncSession,
    version: BillingRuleVersion,
    fields: VersionFields,
    *,
    document_id: uuid.UUID | None,
) -> None:
    if version.status not in (DRAFT, REJECTED):
        raise FinanceRuleError(
            "version_not_editable",
            "Yalnız taslak ya da reddedilmiş sürüm düzenlenebilir; yürürlükteki sürüm için yeni "
            "sürüm açın.",
            conflict=True,
        )
    await _validated(session, fields, document_id)
    _write(version, fields, document_id)
    version.status = DRAFT
    await session.flush()


# --- Deneme hesabı ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrialResult:
    simulation: Simulation
    period_total: Decimal
    annual_total: Decimal
    budget_income_target: Decimal | None  # yürürlükteki yıllık bütçenin gelir hedefi (FIN-17)


async def trial(
    session: AsyncSession,
    rule: RuleInput,
    fields: VersionFields,
    *,
    version_id: uuid.UUID | None = None,
) -> TrialResult:
    """Gerçek veriyle, kayıt oluşturmadan (FIN-02 adım 6). Ödeyenden bağımsız bölüm tutarları."""
    from site_yonetim.services import annual_budgets
    from site_yonetim.services.charging import _units

    errors = check_version(fields)
    if errors:
        raise FormFieldError(errors)
    payer = await session.scalar(
        select(ChargeType.payer_rule).where(ChargeType.id == rule.charge_type_id)
    )
    item = to_item(
        version_id or uuid.uuid7(),
        " ".join(rule.name.split()) or "Aidat",
        fields,
        payer_rule=PayerRule(payer) if payer else PayerRule.OCCUPANT,
        owner_item=rule.owner_item,
    )
    data = ChargeInput(
        charge_date=fields.effective.first_day,
        due_date=fields.effective.first_day,
        items=[item],
        units=await _units(session),
        parties=[],
        accounts=[],
    )
    result = simulate(data, item)
    period_total = result.total.distributed
    target = await annual_budgets.income_target(session, fields.effective.year)
    return TrialResult(
        result, period_total, period_total * periods_per_year(fields.frequency), target
    )


# --- Onaya gönderme ve yürürlük ----------------------------------------------------------


async def _overlap(
    session: AsyncSession, rule: BillingRule, fields: VersionFields
) -> BillingRule | None:
    """Aynı kalem tipinde, yürürlük döneminde ya da sonrasında geçerli kapsamı çakışan kural."""
    others = list(
        await session.scalars(
            select(BillingRule).where(
                BillingRule.charge_type_id == rule.charge_type_id, BillingRule.id != rule.id
            )
        )
    )
    versions = await versions_of(session, [o.id for o in others])
    for other in others:
        candidates = [effective_version(versions[other.id], fields.effective)] + [
            v
            for v in versions[other.id]
            if v.status == ACTIVE
            and YearMonth(v.effective_year, v.effective_month) > fields.effective
        ]
        for v in candidates:
            if v is not None and not v.discontinued and scopes_overlap(fields_of(v), fields):
                return other
    return None


@dataclass(frozen=True, slots=True)
class Submitted:
    version: BillingRuleVersion
    request: ApprovalRequest | None


async def submit(
    session: AsyncSession,
    site: Site,
    rule: BillingRule,
    version: BillingRuleVersion,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> Submitted:
    if version.status not in (DRAFT, REJECTED):
        raise FinanceRuleError(
            "version_not_draft", "Yalnız taslak sürüm onaya gönderilebilir.", conflict=True
        )
    fields = fields_of(version)
    if fields.effective < YearMonth.of(today):
        raise FormFieldError(
            {"effective_period": "Yürürlük dönemi geçmiş olamaz; bu ay ya da sonrası seçin."}
        )
    # Yürürlük kilitli döneme düşemez (FIN-02 adım 3 → PERIOD_LOCKED).
    await period_lock.ensure_writable(session, fields.effective.first_day)
    if not fields.discontinued:
        trial_result = await trial(
            session,
            RuleInput(rule.name, rule.charge_type_id, rule.owner_item),
            fields,
            version_id=version.id,
        )
        missing = [
            w for w in trial_result.simulation.warnings if w.kind is WarningKind.MISSING_WEIGHT_DATA
        ]
        if missing:
            raise FinanceRuleError(
                "missing_unit_data",
                f"{missing[0].message} Kuralı uygulamadan önce bölüm verisini tamamlayın.",
                conflict=True,
            )
        clash = await _overlap(session, rule, fields)
        if clash is not None:
            raise FinanceRuleError(
                "scope_overlap",
                f"Bu kalem tipi için aynı kapsamda yürürlükte başka kural var ('{clash.name}').",
                conflict=True,
            )
    needs_approval = (
        await approvals_svc.match_rule(
            session, ActionType.CHARGE_RULE, amount=fields.amount, source=None, today=today
        )
        is not None
    )
    if not needs_approval:
        version.status = ACTIVE
        version.activated_at = now
        await session.flush()
        return Submitted(version, None)
    version.status = PENDING
    await session.flush()
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CHARGE_RULE,
            title=f"Aidat kuralı — {rule.name} (sürüm {version.version_no}, "
            f"{period_label(fields.effective)} döneminden)",
            summary={
                "rule": rule.name,
                "version": version.version_no,
                "effective_period": f"{fields.effective.year}-{fields.effective.month:02d}",
                "kind": fields.kind.value,
                "amount": str(fields.amount),
                "basis_note": version.basis_note,
                "discontinued": fields.discontinued,
            },
            payload={"version_id": str(version.id)},
            amount=fields.amount,
            subject_type="billing_rule_versions",
            subject_id=version.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    version.approval_request_id = request.id
    await session.flush()
    return Submitted(version, request)


async def _version_of(session: AsyncSession, request: ApprovalRequest) -> BillingRuleVersion | None:
    return await session.get(BillingRuleVersion, uuid.UUID((request.payload or {})["version_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    version = await _version_of(session, request)
    if version is None or version.status != PENDING:
        raise ExecutionError("Aidat kuralı sürümü artık onay beklemiyor.")
    version.status = ACTIVE
    version.activated_at = request.decided_at
    await session.flush()


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    version = await _version_of(session, request)
    if version is not None and version.status == PENDING:
        version.status = REJECTED
        await session.flush()


approvals_svc.register_executor(ActionType.CHARGE_RULE, _execute)
approvals_svc.register_closer(ActionType.CHARGE_RULE, _close)
