"""Sözleşme performansı — servis istekleri 68–70 (Yazılım Dokümanı 15.3, SAT-06). Açık site
kapsamında; transaction'ı çağıran yönetir.

- **Tanımlar** (68): KPI, ceza kuralı ve artış kuralı tek PUT ile kaydedilir. KPI kaldırılınca
  ceza kuralları da kalkar. Tedarikçi kartı bağı (`vendor_id`) saklanır; P10 satın alma paketi
  birleşince kartın varlığı ve puanı doğrulanacak.
- **Karne** (69): taslak her açılışta kaynaklardan yeniden hesaplanır (GET yazmaz); elle girilen
  değer ve yorum taslak satırında saklanır, kaynaklı değer elle ezilemez. Kesinleşen karne değişmez.
  Ceza tetiklenirse kesinti kaydı "onay bekliyor" olur; "Ceza kesintisi" onay kuralı varsa onay
  mekanizmasına (ONY) düşer, yoksa karneyi kesinleştirmeyen bir `contracts.manage` yetkilisi
  karar verir. Karneyi kesinleştiren kesintiyi onaylayamaz.
- **Artış** (70): artış tarihinden 30 gün önce yeni bedel hesaplanır; onayda bedel geçmişine artış
  tarihinden geçerli satır yazılır ("Sözleşme artışı" kuralı varsa ONY'den sonra). Ret → bedel
  değişmez, görüşme notu saklanır.
- **Dönem kilidi**: onaylanan kesinti karne döneminin, onaylanan artış artış tarihinin dönemine
  yazılır (`period_lock.ensure_writable`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType, ApprovalStatus, Decision
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.contract_performance import (
    COMMENT_MAX,
    DUE_DAYS,
    NOTE_MIN,
    AmountSource,
    CutKind,
    Definitions,
    Escalation,
    EscalationInput,
    EscalationKind,
    EscalationPeriod,
    EscalationStatus,
    KpiInput,
    KpiSource,
    PenaltyInput,
    PenaltyResult,
    PenaltyRule,
    PenaltyStatus,
    PreviewStatus,
    ScorecardStatus,
    ScoreRow,
    annual_effect,
    capped,
    check_definitions,
    check_index,
    clean_note,
    compute_penalty,
    escalated_amount,
    escalation_date,
    index_period,
    index_rate,
    monthly_amount,
    number_tr,
    percent,
    total_score,
)
from site_yonetim.domain.facility import CompletionStatus, task_applies
from site_yonetim.domain.facility import TaskFrequency as _TaskFrequency
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.fiscal import parse_period_key
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import RequestStatus
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    ApprovalDecision,
    ApprovalRequest,
    AssetInspectionRecord,
    ChecklistInspection,
    Contract,
    ContractEscalation,
    ContractKpi,
    ContractPenalty,
    ContractPenaltyRule,
    ContractScorecard,
    IndexValue,
    PatrolRun,
    PatrolScan,
    Request,
    RequestCategory,
    Site,
    TaskCompletion,
    TaskTemplate,
    TechnicalAsset,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import contracts as contracts_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import period_lock
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.roles import permission_holders

JOB_ACTOR = "Sözleşme işleri"
SCORECARDS_SHOWN = 12


class PerformanceError(Exception):
    """İş kuralı ihlali: `status` 403/404/409/422, `code` makine için, mesaj Türkçe."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- Tanımlar (68) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Loaded:
    kpis: list[ContractKpi]
    penalties: list[ContractPenaltyRule]


async def load(session: AsyncSession, contract_id: uuid.UUID) -> Loaded:
    kpis = list(
        await session.scalars(
            select(ContractKpi)
            .where(ContractKpi.contract_id == contract_id)
            .order_by(ContractKpi.position, ContractKpi.id)
        )
    )
    penalties = list(
        await session.scalars(
            select(ContractPenaltyRule)
            .where(ContractPenaltyRule.contract_id == contract_id)
            .order_by(ContractPenaltyRule.position, ContractPenaltyRule.id)
        )
    )
    return Loaded(kpis, penalties)


def escalation_of(contract: Contract) -> Escalation:
    return Escalation(
        kind=EscalationKind(contract.escalation_kind),
        fixed_rate=contract.escalation_fixed_rate,
        period=EscalationPeriod(contract.escalation_period),
        cap=contract.escalation_cap,
    )


def _rules(loaded: Loaded) -> list[PenaltyRule]:
    return [
        PenaltyRule(str(p.kpi_id), p.below_percent, CutKind(p.cut_kind), p.cut_value)
        for p in loaded.penalties
    ]


@dataclass(frozen=True, slots=True)
class DefinitionsInput:
    vendor_id: uuid.UUID | None
    vendor_given: bool
    kpis: list[KpiInput] | None
    penalties: list[PenaltyInput] | None
    escalation: EscalationInput | None


async def save_definitions(
    session: AsyncSession, contract: Contract, data: DefinitionsInput, *, today: date
) -> None:
    """KPI, ceza ve artış kurallarını doğrulayıp kaydeder; hatalar `FormFieldError`."""
    loaded = await load(session, contract.id)
    kpi_inputs = (
        data.kpis
        if data.kpis is not None
        else [
            KpiInput(str(k.id), k.name, k.source, k.target, k.weight, k.request_category)
            for k in loaded.kpis
        ]
    )
    penalty_inputs = (
        data.penalties
        if data.penalties is not None
        else [
            PenaltyInput(str(p.kpi_id), p.below_percent, p.cut_kind, p.cut_value)
            for p in loaded.penalties
        ]
    )
    current = escalation_of(contract)
    escalation = data.escalation or EscalationInput(
        current.kind.value, current.fixed_rate, current.period.value, current.cap
    )
    rows = await contracts_svc.amount_rows(session, contract.id)
    amount, period = contracts_svc.amount_at(rows, today) if rows else (None, None)
    categories = set(await session.scalars(select(RequestCategory.key)))
    clean, errors = check_definitions(
        kpi_inputs,
        penalty_inputs,
        escalation,
        has_amount=contract.amount is not None,
        has_monthly_amount=monthly_amount(amount, period) is not None
        or monthly_amount(contract.amount, contract.period) is not None,
        request_categories=categories,
    )
    if errors:
        raise FormFieldError(errors)
    await _write(session, contract, clean, loaded)
    if data.vendor_given:
        contract.vendor_id = data.vendor_id
    contract.escalation_kind = clean.escalation.kind.value
    contract.escalation_fixed_rate = clean.escalation.fixed_rate
    contract.escalation_period = clean.escalation.period.value
    contract.escalation_cap = clean.escalation.cap
    await session.flush()


async def _write(
    session: AsyncSession, contract: Contract, clean: Definitions, loaded: Loaded
) -> None:
    existing = {str(k.id): k for k in loaded.kpis}
    ids: dict[str, uuid.UUID] = {}
    kept: set[uuid.UUID] = set()
    for position, kpi in enumerate(clean.kpis):
        row = existing.get(kpi.key)
        if row is None:
            row = ContractKpi(id=uuid.uuid7(), contract_id=contract.id)
            session.add(row)
        row.position = position
        row.name = kpi.name
        row.source = kpi.source.value
        row.target = kpi.target
        row.weight = kpi.weight
        row.request_category = kpi.request_category
        ids[kpi.key] = row.id
        kept.add(row.id)
    for penalty in loaded.penalties:
        await session.delete(penalty)
    await session.flush()
    for row in loaded.kpis:
        if row.id not in kept:
            await session.delete(row)
    await session.flush()
    for position, rule in enumerate(clean.penalties):
        session.add(
            ContractPenaltyRule(
                id=uuid.uuid7(),
                contract_id=contract.id,
                kpi_id=ids[rule.kpi_key],
                position=position,
                below_percent=rule.below_percent,
                cut_kind=rule.cut_kind.value,
                cut_value=rule.cut_value,
            )
        )
    await session.flush()


# --- KPI kaynakları -------------------------------------------------------------------------


def _bounds(period: YearMonth) -> tuple[datetime, datetime]:
    start = datetime.combine(period.first_day, time(0), tzinfo=BUSINESS_TZ)
    end = datetime.combine(period.next().first_day, time(0), tzinfo=BUSINESS_TZ)
    return start, end


@dataclass(frozen=True, slots=True)
class Measured:
    actual: Decimal | None
    detail: str
    editable: bool  # kaynak yok / kapalı / veri yok → elle girilir


async def _inspection(session: AsyncSession, period: YearMonth) -> Measured:
    start, end = _bounds(period)
    count, avg = (
        await session.execute(
            select(func.count(), func.avg(ChecklistInspection.score)).where(
                ChecklistInspection.created_at >= start, ChecklistInspection.created_at < end
            )
        )
    ).one()
    if not count:
        return Measured(None, "Bu ay denetim yok — elle girin", True)
    value = Decimal(avg).quantize(Decimal(1), ROUND_HALF_UP)
    return Measured(value, f"{count} denetim ortalaması", False)


async def _patrol(session: AsyncSession, period: YearMonth) -> Measured:
    runs_in = and_(PatrolRun.run_date >= period.first_day, PatrolRun.run_date <= period.last_day)
    runs, planned = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(func.cardinality(PatrolRun.point_ids)), 0))
            .select_from(PatrolRun)
            .where(runs_in)
        )
    ).one()
    scanned = await session.scalar(
        select(func.count())
        .select_from(PatrolScan)
        .join(
            PatrolRun,
            and_(PatrolRun.id == PatrolScan.run_id, PatrolRun.site_id == PatrolScan.site_id),
        )
        .where(runs_in)
    )
    if not planned:
        return Measured(None, "Bu ay devriye yok — elle girin", True)
    done = min(int(scanned or 0), int(planned))
    return Measured(percent(done, int(planned)), f"{runs} tur, {done}/{planned} nokta", False)


async def _tasks(session: AsyncSession, period: YearMonth, today: date) -> Measured:
    last = min(period.last_day, today - timedelta(days=1))
    templates = list(await session.scalars(select(TaskTemplate).where(TaskTemplate.is_active)))
    expected = 0
    day = period.first_day
    while day <= last:
        for t in templates:
            if day >= t.created_at.astimezone(BUSINESS_TZ).date() and task_applies(
                _TaskFrequency(t.frequency), list(t.weekdays), t.month_day, day
            ):
                expected += 1
        day += timedelta(days=1)
    if not expected:
        return Measured(None, "Bu ay görev yok — elle girin", True)
    done = await session.scalar(
        select(func.count()).where(
            TaskCompletion.task_date >= period.first_day,
            TaskCompletion.task_date <= last,
            TaskCompletion.status == CompletionStatus.DONE.value,
            TaskCompletion.template_id.in_([t.id for t in templates]),
        )
    )
    done_count = min(int(done or 0), expected)
    return Measured(percent(done_count, expected), f"{done_count}/{expected} görev", False)


async def _sla(
    session: AsyncSession, period: YearMonth, category: str | None, modules: frozenset[ModuleKey]
) -> Measured:
    if ModuleKey.REQUESTS not in modules:
        return Measured(None, "Talep modülü kapalı — elle girin", True)
    start, end = _bounds(period)
    query = select(func.count(), func.count().filter(Request.resolved_at <= Request.due_at)).where(
        Request.status.in_([RequestStatus.RESOLVED.value, RequestStatus.CLOSED.value]),
        Request.resolved_at >= start,
        Request.resolved_at < end,
        Request.due_at.is_not(None),
    )
    if category:
        query = query.where(Request.category == category)
    total, met = (await session.execute(query)).one()
    if not total:
        return Measured(None, "Bu ay çözülen talep yok — elle girin", True)
    return Measured(percent(int(met), int(total)), f"{met}/{total} talep SLA içinde", False)


async def _asset_inspection(
    session: AsyncSession, period: YearMonth, contract_id: uuid.UUID
) -> Measured:
    total, passed = (
        await session.execute(
            select(func.count(), func.count().filter(AssetInspectionRecord.result == "pass"))
            .select_from(AssetInspectionRecord)
            .join(
                TechnicalAsset,
                and_(
                    TechnicalAsset.id == AssetInspectionRecord.asset_id,
                    TechnicalAsset.site_id == AssetInspectionRecord.site_id,
                ),
            )
            .where(
                TechnicalAsset.contract_id == contract_id,
                AssetInspectionRecord.inspected_on >= period.first_day,
                AssetInspectionRecord.inspected_on <= period.last_day,
            )
        )
    ).one()
    if not total:
        return Measured(None, "Bu ay sözleşmeye bağlı varlık muayenesi yok — elle girin", True)
    return Measured(percent(int(passed), int(total)), f"{passed}/{total} muayene olumlu", False)


async def measure(
    session: AsyncSession,
    kpi: ContractKpi,
    contract: Contract,
    period: YearMonth,
    *,
    today: date,
    modules: frozenset[ModuleKey],
) -> Measured:
    source = KpiSource(kpi.source)
    if source is KpiSource.INSPECTION:
        return await _inspection(session, period)
    if source is KpiSource.PATROL:
        return await _patrol(session, period)
    if source is KpiSource.TASKS:
        return await _tasks(session, period, today)
    if source is KpiSource.SLA:
        return await _sla(session, period, kpi.request_category, modules)
    if source is KpiSource.ASSET_INSPECTION:
        return await _asset_inspection(session, period, contract.id)
    return Measured(None, "Elle girilir", True)


# --- Karne (69) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardRow:
    kpi_id: str
    name: str
    source: KpiSource
    target: Decimal
    weight: int
    actual: Decimal | None
    score: int | None
    detail: str | None
    editable: bool


@dataclass(frozen=True, slots=True)
class CardView:
    contract: Contract
    period: str
    card: ContractScorecard | None
    rows: list[CardRow]
    total: int | None
    comment: str | None
    status: ScorecardStatus
    monthly_amount: Decimal | None
    computed_penalty: PenaltyResult | None  # taslakta hesaplanan (henüz kayıt yok)
    penalty: ContractPenalty | None

    @property
    def expected_invoice(self) -> Decimal | None:
        if self.monthly_amount is None:
            return None
        cut = (
            self.penalty.amount
            if self.penalty is not None and self.penalty.status == PenaltyStatus.APPROVED.value
            else Decimal(0)
        )
        return self.monthly_amount - cut


def parse_period(value: str) -> YearMonth:
    parsed = parse_period_key(value)
    if parsed is None:
        raise FormFieldError({"period": "Dönem seçin (YYYY-AA)."})
    return parsed


def check_period(contract: Contract, period: YearMonth, today: date) -> None:
    if period.first_day > today:
        raise FormFieldError({"period": "Gelecek dönem seçilemez."})
    if period.last_day < contract.start_date or period.first_day > contract.end_date:
        raise FormFieldError({"period": "Dönem sözleşme süresi dışında."})


async def get_card(
    session: AsyncSession, contract_id: uuid.UUID, period: str, *, lock: bool = False
) -> ContractScorecard | None:
    query = select(ContractScorecard).where(
        ContractScorecard.contract_id == contract_id, ContractScorecard.period == period
    )
    found: ContractScorecard | None = await session.scalar(
        query.with_for_update() if lock else query
    )
    return found


async def penalty_of(
    session: AsyncSession, card: ContractScorecard | None
) -> ContractPenalty | None:
    if card is None:
        return None
    found: ContractPenalty | None = await session.scalar(
        select(ContractPenalty).where(ContractPenalty.scorecard_id == card.id)
    )
    return found


async def _monthly_at(session: AsyncSession, contract: Contract, day: date) -> Decimal | None:
    rows = await contracts_svc.amount_rows(session, contract.id)
    amount, period = (
        contracts_svc.amount_at(rows, day) if rows else (contract.amount, contract.period)
    )
    return monthly_amount(amount, period)


def _num(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _frozen_rows(card: ContractScorecard) -> list[CardRow]:
    out = []
    for r in card.rows or []:
        row = ScoreRow(
            r["kpi_id"],
            r["name"],
            KpiSource(r["source"]),
            Decimal(str(r["target"])),
            int(r["weight"]),
            _num(r.get("actual")),
            r.get("detail"),
        )
        out.append(
            CardRow(
                row.kpi_key,
                row.name,
                row.source,
                row.target,
                row.weight,
                row.actual,
                row.score,
                row.detail,
                False,
            )
        )
    return out


async def view(
    session: AsyncSession,
    contract: Contract,
    period: YearMonth,
    *,
    today: date,
    modules: frozenset[ModuleKey],
    card: ContractScorecard | None = None,
    loaded: Loaded | None = None,
) -> CardView:
    key = f"{period.year:04d}-{period.month:02d}"
    if card is None:
        card = await get_card(session, contract.id, key)
    penalty = await penalty_of(session, card)
    if card is not None and card.status == ScorecardStatus.FINALIZED.value:
        rows = _frozen_rows(card)
        return CardView(
            contract,
            key,
            card,
            rows,
            card.total,
            card.comment,
            ScorecardStatus.FINALIZED,
            card.monthly_amount,
            None,
            penalty,
        )
    loaded = loaded or await load(session, contract.id)
    manual = card.manual_values if card is not None else {}
    score_rows: list[ScoreRow] = []
    editable: list[bool] = []
    for kpi in loaded.kpis:
        m = await measure(session, kpi, contract, period, today=today, modules=modules)
        actual = m.actual if m.actual is not None else _num(manual.get(str(kpi.id)))
        score_rows.append(
            ScoreRow(
                str(kpi.id),
                kpi.name,
                KpiSource(kpi.source),
                kpi.target,
                kpi.weight,
                actual,
                m.detail,
            )
        )
        editable.append(m.editable)
    monthly = await _monthly_at(session, contract, period.first_day)
    rows = [
        CardRow(r.kpi_key, r.name, r.source, r.target, r.weight, r.actual, r.score, r.detail, e)
        for r, e in zip(score_rows, editable, strict=True)
    ]
    computed = compute_penalty(score_rows, _rules(loaded), monthly)
    return CardView(
        contract,
        key,
        card,
        rows,
        total_score(score_rows),
        card.comment if card is not None else None,
        ScorecardStatus.DRAFT,
        monthly,
        computed,
        penalty,
    )


async def save_values(
    session: AsyncSession,
    contract: Contract,
    period: YearMonth,
    *,
    values: dict[str, Decimal | None] | None,
    comment: str | None,
    comment_given: bool,
    today: date,
    modules: frozenset[ModuleKey],
) -> CardView:
    """Elle girilebilen KPI değerleri ve yorum (taslak). Kaynaklı değer ezilemez."""
    key = f"{period.year:04d}-{period.month:02d}"
    card = await get_card(session, contract.id, key, lock=True)
    if card is not None and card.status != ScorecardStatus.DRAFT.value:
        raise PerformanceError(409, "finalized", "Kesinleşmiş karne değiştirilemez.")
    current = await view(session, contract, period, today=today, modules=modules, card=card)
    by_id = {r.kpi_id: r for r in current.rows}
    errors: dict[str, str] = {}
    manual = dict(card.manual_values) if card is not None else {}
    for kpi_id, value in (values or {}).items():
        row = by_id.get(kpi_id)
        if row is None:
            errors[f"values.{kpi_id}"] = "KPI bulunamadı."
        elif not row.editable:
            errors[f"values.{kpi_id}"] = "Bu KPI kaynaktan hesaplanır; elle girilemez."
        elif value is not None and not (Decimal(0) <= value <= Decimal(100)):
            errors[f"values.{kpi_id}"] = "0–100."
        elif value is not None and value != value.quantize(Decimal("0.01")):
            errors[f"values.{kpi_id}"] = "En çok 2 ondalık."
        elif value is None:
            manual.pop(kpi_id, None)
        else:
            manual[kpi_id] = str(value)
    if comment_given and comment is not None and len(" ".join(comment.split())) > COMMENT_MAX:
        errors["comment"] = f"Yorum en çok {COMMENT_MAX} karakter."
    if errors:
        raise FormFieldError(errors)
    if card is None:
        card = ContractScorecard(
            id=uuid.uuid7(),
            contract_id=contract.id,
            period=key,
            status=ScorecardStatus.DRAFT.value,
            manual_values={},
        )
        session.add(card)
    card.manual_values = manual
    if comment_given:
        card.comment = " ".join((comment or "").split()) or None
    await session.flush()
    return await view(session, contract, period, today=today, modules=modules, card=card)


@dataclass(frozen=True, slots=True)
class Finalized:
    view: CardView
    request: ApprovalRequest | None


async def finalize(
    session: AsyncSession,
    site: Site,
    contract: Contract,
    period: YearMonth,
    *,
    actor: Person,
    today: date,
    now: datetime,
    modules: frozenset[ModuleKey],
) -> Finalized:
    key = f"{period.year:04d}-{period.month:02d}"
    card = await get_card(session, contract.id, key, lock=True)
    if card is not None and card.status != ScorecardStatus.DRAFT.value:
        raise PerformanceError(409, "finalized", "Karne zaten kesinleşti.")
    if period.last_day >= today:
        raise PerformanceError(409, "period_open", "Dönem bitmeden karne kesinleşmez.")
    loaded = await load(session, contract.id)
    if not loaded.kpis:
        raise PerformanceError(409, "no_kpis", "KPI tanımlı değil; önce KPI'ları girin.")
    current = await view(
        session, contract, period, today=today, modules=modules, card=card, loaded=loaded
    )
    missing = [r.name for r in current.rows if r.actual is None]
    if missing:
        raise PerformanceError(
            422, "missing_values", f"Gerçekleşeni eksik KPI: {', '.join(missing)}."
        )
    if card is None:
        card = ContractScorecard(
            id=uuid.uuid7(), contract_id=contract.id, period=key, manual_values={}
        )
        session.add(card)
    card.rows = [
        {
            "kpi_id": r.kpi_id,
            "name": r.name,
            "source": r.source.value,
            "target": str(r.target),
            "weight": r.weight,
            "actual": None if r.actual is None else str(r.actual),
            "detail": r.detail,
        }
        for r in current.rows
    ]
    card.total = current.total
    card.monthly_amount = current.monthly_amount
    card.status = ScorecardStatus.FINALIZED.value
    card.finalized_by_id = actor.user_id
    card.finalized_by = actor.name
    card.finalized_at = now
    await session.flush()
    request = None
    computed = current.computed_penalty
    if computed is not None and computed.amount > 0:
        penalty = ContractPenalty(
            id=uuid.uuid7(),
            contract_id=contract.id,
            scorecard_id=card.id,
            period=key,
            amount=computed.amount,
            reasons=computed.reasons,
            status=PenaltyStatus.PENDING.value,
            requested_by_id=actor.user_id,
        )
        session.add(penalty)
        await session.flush()
        version = await approvals_svc.match_rule(
            session, ActionType.PENALTY_DEDUCTION, amount=computed.amount, source=None, today=today
        )
        if version is not None:
            request = await approvals_svc.request_approval(
                session,
                site,
                approvals_svc.NewRequest(
                    action_type=ActionType.PENALTY_DEDUCTION,
                    title=f"Ceza kesintisi — {contract.vendor}, {period.name}",
                    summary={
                        "vendor": contract.vendor,
                        "subject": contract.subject,
                        "period": key,
                        "amount": f"{computed.amount:.2f}",
                        "reasons": computed.reasons,
                    },
                    payload={"penalty_id": str(penalty.id)},
                    amount=computed.amount,
                    subject_type="contract_penalties",
                    subject_id=penalty.id,
                ),
                requester=actor,
                today=today,
                now=now,
            )
            penalty.approval_request_id = request.id
            await session.flush()
        else:
            managers = await permission_holders(session, site, Permission.CONTRACTS_MANAGE, now)
            await notify_svc.notify(
                session,
                managers - {actor.user_id},
                notify_svc.Message(
                    category=NotificationCategory.OPERATIONS,
                    kind="contract.penalty_pending",
                    title=f"Ceza kesintisi onay bekliyor: {contract.vendor}",
                    body=f"{period.name} karnesi · {format_money_tr(computed.amount)}",
                    link_entity="contracts",
                    link_id=contract.id,
                ),
                dedupe_key=f"contract.penalty:{penalty.id}",
            )
    out = await view(
        session, contract, period, today=today, modules=modules, card=card, loaded=loaded
    )
    return Finalized(out, request)


def _reject_note(approve: bool, note: str | None) -> str | None:
    text = clean_note(note)
    if not approve and (text is None or len(text) < NOTE_MIN):
        raise FormFieldError({"note": "Ret gerekçesi yazın (en az 5 karakter)."})
    return text


async def _ensure_open(session: AsyncSession, day: date, now: datetime) -> None:
    await period_lock.ensure_writable(session, day, now=now)


async def decide_penalty(
    session: AsyncSession,
    site: Site,
    contract: Contract,
    period: YearMonth,
    *,
    approve: bool,
    note: str | None,
    actor: Person,
    can_manage: bool,
    now: datetime,
) -> ApprovalRequest | None:
    """Onay kuralı varsa kararı ONY'ye yazar (yetki ve sıra orada); yoksa `contracts.manage`."""
    key = f"{period.year:04d}-{period.month:02d}"
    card = await get_card(session, contract.id, key)
    penalty: ContractPenalty | None = None
    if card is not None:
        penalty = await session.scalar(
            select(ContractPenalty).where(ContractPenalty.scorecard_id == card.id).with_for_update()
        )
    if penalty is None or card is None:
        raise PerformanceError(404, "not_found", "Ceza kesintisi bulunamadı.")
    if penalty.status != PenaltyStatus.PENDING.value:
        raise PerformanceError(409, "invalid_status", "Onay bekleyen kesinti yok.")
    if card.finalized_by_id == actor.user_id:
        raise PerformanceError(
            403, "self_approval", "Karneyi kesinleştiren kişi kesintiyi onaylayamaz."
        )
    text = _reject_note(approve, note)
    if penalty.approval_request_id is not None:
        request = await approvals_svc.get_request(session, penalty.approval_request_id, lock=True)
        if request is not None and ApprovalStatus(request.status) in OPEN_STATUSES:
            await approvals_svc.decide(
                session,
                site,
                request,
                decider=actor,
                decision=Decision.APPROVE if approve else Decision.REJECT,
                comment=text,
                now=now,
            )
            await session.refresh(penalty)
            return request
    if not can_manage:
        raise PerformanceError(403, "forbidden", "Bu işlem için yetkiniz yok.")
    if approve:
        await _ensure_open(session, period.last_day, now)
    penalty.status = (PenaltyStatus.APPROVED if approve else PenaltyStatus.REJECTED).value
    penalty.decided_by = actor.name
    penalty.decided_by_id = actor.user_id
    penalty.decided_at = now
    penalty.note = text
    await session.flush()
    return None


def supplier_notice(contract: Contract, view_: CardView) -> str | None:
    p = view_.penalty
    if p is None or p.status != PenaltyStatus.APPROVED.value or view_.expected_invoice is None:
        return None
    period = parse_period_key(view_.period)
    label = period.name if period else view_.period
    return (
        f"Sayın {contract.vendor},\n{label} dönemi hizmet karnesinde {'; '.join(p.reasons)}. "
        f"Sözleşmedeki ceza maddesi gereği {format_money_tr(p.amount)} kesinti uygulanmıştır; "
        f"bu dönem faturanızı {format_money_tr(view_.expected_invoice)} olarak "
        "düzenlemenizi rica ederiz."
    )


async def last_cards(
    session: AsyncSession, contract_id: uuid.UUID
) -> list[tuple[ContractScorecard, ContractPenalty | None]]:
    cards = list(
        await session.scalars(
            select(ContractScorecard)
            .where(ContractScorecard.contract_id == contract_id)
            .order_by(ContractScorecard.period.desc())
            .limit(SCORECARDS_SHOWN)
        )
    )
    penalties = (
        {
            p.scorecard_id: p
            for p in await session.scalars(
                select(ContractPenalty).where(
                    ContractPenalty.scorecard_id.in_([c.id for c in cards])
                )
            )
        }
        if cards
        else {}
    )
    return [(c, penalties.get(c.id)) for c in cards]


# --- Endeks değerleri (platform) ------------------------------------------------------------


def latest_index_query() -> Select[IndexValue]:
    """Her dönemin en son girilen değeri."""
    return (
        select(IndexValue)
        .ext(distinct_on(IndexValue.period))
        .order_by(IndexValue.period.desc(), IndexValue.created_at.desc(), IndexValue.id.desc())
    )


async def index_for(session: AsyncSession, period: str) -> IndexValue | None:
    found: IndexValue | None = await session.scalar(
        select(IndexValue)
        .where(IndexValue.period == period)
        .order_by(IndexValue.created_at.desc(), IndexValue.id.desc())
        .limit(1)
    )
    return found


async def save_index(
    session: AsyncSession,
    period: str,
    tufe: Decimal | None,
    ufe: Decimal | None,
    *,
    actor: Person,
    today: date,
) -> IndexValue:
    errors = check_index(period, tufe, ufe, this_month=f"{today.year:04d}-{today.month:02d}")
    if errors or tufe is None or ufe is None:
        raise FormFieldError(errors)
    row = IndexValue(
        id=uuid.uuid7(),
        period=period,
        tufe=tufe,
        ufe=ufe,
        entered_by_id=actor.user_id,
        entered_by=actor.name,
    )
    session.add(row)
    await session.flush()
    return row


# --- Endeksli artış (70) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preview:
    status: PreviewStatus
    date: date | None = None
    days_left: int | None = None
    index_period: str | None = None
    rate: Decimal | None = None
    capped_rate: Decimal | None = None
    old_amount: Decimal | None = None
    new_amount: Decimal | None = None
    annual_effect: Decimal | None = None
    record: ContractEscalation | None = None


async def _active_record(
    session: AsyncSession, contract_id: uuid.UUID, day: date, *, lock: bool = False
) -> ContractEscalation | None:
    query = select(ContractEscalation).where(
        ContractEscalation.contract_id == contract_id,
        ContractEscalation.due_date == day,
        ContractEscalation.status != EscalationStatus.WITHDRAWN.value,
    )
    found: ContractEscalation | None = await session.scalar(
        query.with_for_update() if lock else query
    )
    return found


async def _stale(session: AsyncSession, record: ContractEscalation) -> bool:
    """Bekleyen kaydın onay talebi artık açık değil (yürütülemedi) → yeniden karar verilebilir."""
    if record.status != EscalationStatus.PENDING.value or record.approval_request_id is None:
        return False
    request = await session.get(ApprovalRequest, record.approval_request_id)
    return request is None or ApprovalStatus(request.status) not in OPEN_STATUSES


async def preview(session: AsyncSession, contract: Contract, *, today: date) -> Preview:
    rule = escalation_of(contract)
    if rule.kind is EscalationKind.NONE or contract.amount is None or contract.is_archived:
        return Preview(PreviewStatus.NONE)
    day = escalation_date(contract.start_date, contract.end_date, rule.period, today)
    if day is None:
        return Preview(PreviewStatus.NONE)
    days_left = (day - today).days
    idx_period = index_period(day)
    record = await _active_record(session, contract.id, day)
    if record is not None and not await _stale(session, record):
        rows = await contracts_svc.amount_rows(session, contract.id)
        amount_period = contracts_svc.amount_at(rows, day - timedelta(days=1))[1] if rows else None
        status = {
            EscalationStatus.PENDING.value: PreviewStatus.PENDING,
            EscalationStatus.APPROVED.value: PreviewStatus.APPROVED,
            EscalationStatus.REJECTED.value: PreviewStatus.REJECTED,
        }[record.status]
        new = record.new_amount if status is not PreviewStatus.REJECTED else record.old_amount
        return Preview(
            status,
            day,
            days_left,
            record.index_period,
            record.rate,
            record.capped_rate,
            record.old_amount,
            record.new_amount,
            annual_effect(record.old_amount, new, amount_period or contract.period),
            record,
        )
    rows = await contracts_svc.amount_rows(session, contract.id)
    old, period = (
        contracts_svc.amount_at(rows, day - timedelta(days=1))
        if rows
        else (contract.amount, contract.period)
    )
    if old is None:
        return Preview(PreviewStatus.NONE)
    rate: Decimal | None
    if rule.kind is EscalationKind.FIXED:
        rate = rule.fixed_rate
        shown_period = None
    else:
        index = await index_for(session, idx_period)
        rate = index_rate(rule.kind, index.tufe, index.ufe) if index else None
        shown_period = idx_period
    if rate is None:
        status = PreviewStatus.NOT_DUE if days_left > DUE_DAYS else PreviewStatus.MISSING_INDEX
        return Preview(status, day, days_left, shown_period, None, None, old)
    limited = capped(rate, rule.cap)
    new = escalated_amount(old, limited)
    status = PreviewStatus.NOT_DUE if days_left > DUE_DAYS else PreviewStatus.DUE
    return Preview(
        status,
        day,
        days_left,
        shown_period,
        rate,
        limited,
        old,
        new,
        annual_effect(old, new, period),
    )


@dataclass(frozen=True, slots=True)
class EscalationDecided:
    record: ContractEscalation
    request: ApprovalRequest | None


async def decide_escalation(
    session: AsyncSession,
    site: Site,
    contract: Contract,
    *,
    approve: bool,
    note: str | None,
    actor: Person,
    today: date,
    now: datetime,
) -> EscalationDecided:
    p = await preview(session, contract, today=today)
    if p.status is PreviewStatus.MISSING_INDEX:
        raise PerformanceError(
            409, "not_due", "Endeks değeri girilmemiş; platform yöneticisine bildirildi."
        )
    if p.status is not PreviewStatus.DUE or p.date is None:
        raise PerformanceError(
            409, "not_due", "Onay bekleyen artış yok (artış tarihinden 30 gün önce açılır)."
        )
    if p.old_amount is None or p.new_amount is None or p.rate is None or p.capped_rate is None:
        raise PerformanceError(409, "not_due", "Artış hesaplanamadı; sözleşme bedelini girin.")
    text = clean_note(note)
    if not approve and (text is None or len(text) < NOTE_MIN):
        raise FormFieldError({"note": "Tedarikçiyle görüşme notunu yazın."})
    stale = await _active_record(session, contract.id, p.date, lock=True)
    if stale is not None:
        stale.status = EscalationStatus.WITHDRAWN.value
        await session.flush()
    record = ContractEscalation(
        id=uuid.uuid7(),
        contract_id=contract.id,
        due_date=p.date,
        kind=contract.escalation_kind,
        index_period=p.index_period,
        rate=p.rate,
        capped_rate=p.capped_rate,
        old_amount=p.old_amount,
        new_amount=p.new_amount,
        status=EscalationStatus.PENDING.value,
        note=text,
        requested_by_id=actor.user_id,
        requested_by=actor.name,
    )
    session.add(record)
    await session.flush()
    if not approve:
        record.status = EscalationStatus.REJECTED.value
        record.new_amount = p.old_amount
        record.decided_by = actor.name
        record.decided_at = now
        await session.flush()
        return EscalationDecided(record, None)
    await _ensure_open(session, p.date, now)
    version = await approvals_svc.match_rule(
        session, ActionType.CONTRACT_ESCALATION, amount=p.annual_effect, source=None, today=today
    )
    if version is None:
        await _apply_escalation(session, contract, record, actor=actor, now=now)
        return EscalationDecided(record, None)
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CONTRACT_ESCALATION,
            title=f"Sözleşme artışı — {contract.vendor}, {p.date:%d.%m.%Y}",
            summary={
                "vendor": contract.vendor,
                "subject": contract.subject,
                "date": p.date.isoformat(),
                "rate": f"{p.capped_rate:.2f}",
                "old_amount": f"{p.old_amount:.2f}",
                "new_amount": f"{p.new_amount:.2f}",
                "annual_effect": f"{(p.annual_effect or Decimal(0)):.2f}",
            },
            payload={"escalation_id": str(record.id)},
            amount=p.annual_effect,
            subject_type="contract_escalations",
            subject_id=record.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    record.approval_request_id = request.id
    await session.flush()
    return EscalationDecided(record, request)


async def _apply_escalation(
    session: AsyncSession,
    contract: Contract,
    record: ContractEscalation,
    *,
    actor: Person | None,
    now: datetime,
) -> None:
    rows = await contracts_svc.amount_rows(session, contract.id)
    period = (
        contracts_svc.amount_at(rows, record.due_date - timedelta(days=1))[1]
        if rows
        else contract.period
    )
    await contracts_svc.record_amount(
        session,
        contract,
        amount=record.new_amount,
        period=period,
        effective_from=record.due_date,
        source=AmountSource.ESCALATION,
        actor=actor,
        note=f"%{number_tr(record.capped_rate)} artış",
        escalation_id=record.id,
    )
    record.status = EscalationStatus.APPROVED.value
    record.decided_by = actor.name if actor else record.decided_by
    record.decided_at = now
    await session.flush()


async def escalation_history(
    session: AsyncSession, contract_id: uuid.UUID
) -> list[ContractEscalation]:
    return list(
        await session.scalars(
            select(ContractEscalation)
            .where(ContractEscalation.contract_id == contract_id)
            .order_by(ContractEscalation.due_date.desc(), ContractEscalation.created_at.desc())
            .limit(50)
        )
    )


# --- Onay mekanizması yürütücüleri -----------------------------------------------------------


async def _deciders(
    session: AsyncSession, request: ApprovalRequest, decision: Decision
) -> list[tuple[str, str | None]]:
    rows = await session.execute(
        select(ApprovalDecision.user_name, ApprovalDecision.comment)
        .where(
            ApprovalDecision.request_id == request.id,
            ApprovalDecision.revision == request.revision,
            ApprovalDecision.decision == decision.value,
        )
        .order_by(ApprovalDecision.created_at)
    )
    return [(n, c) for n, c in rows.all()]


async def _penalty_of_request(
    session: AsyncSession, request: ApprovalRequest
) -> ContractPenalty | None:
    raw = (request.payload or {}).get("penalty_id")
    if raw is None:
        return None
    found: ContractPenalty | None = await session.scalar(
        select(ContractPenalty).where(ContractPenalty.id == uuid.UUID(raw)).with_for_update()
    )
    return found


async def _execute_penalty(session: AsyncSession, request: ApprovalRequest) -> None:
    penalty = await _penalty_of_request(session, request)
    if penalty is None or penalty.status != PenaltyStatus.PENDING.value:
        raise ExecutionError("Ceza kesintisi artık onay beklemiyor.")
    period = parse_period_key(penalty.period)
    try:
        if period is not None:
            await _ensure_open(session, period.last_day, request.decided_at or datetime.now(UTC))
    except FinanceRuleError as exc:
        raise ExecutionError(exc.message) from exc
    names = await _deciders(session, request, Decision.APPROVE)
    penalty.status = PenaltyStatus.APPROVED.value
    penalty.decided_by = ", ".join(n for n, _ in names) or None
    penalty.decided_at = request.decided_at
    penalty.note = next((c for _, c in reversed(names) if c), None)
    await session.flush()


async def _close_penalty(session: AsyncSession, request: ApprovalRequest) -> None:
    penalty = await _penalty_of_request(session, request)
    if penalty is None or penalty.status != PenaltyStatus.PENDING.value:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        last = (await _deciders(session, request, Decision.REJECT))[-1:]
        penalty.status = PenaltyStatus.REJECTED.value
        penalty.decided_by = last[0][0] if last else None
        penalty.note = last[0][1] if last else None
        penalty.decided_at = request.decided_at
    else:
        # Geri çekildi: kesinti bekler, basit akışla (contracts.manage) karara bağlanır.
        penalty.approval_request_id = None
    await session.flush()


async def _escalation_of_request(
    session: AsyncSession, request: ApprovalRequest
) -> ContractEscalation | None:
    raw = (request.payload or {}).get("escalation_id")
    if raw is None:
        return None
    found: ContractEscalation | None = await session.scalar(
        select(ContractEscalation).where(ContractEscalation.id == uuid.UUID(raw)).with_for_update()
    )
    return found


async def _execute_escalation(session: AsyncSession, request: ApprovalRequest) -> None:
    record = await _escalation_of_request(session, request)
    if record is None or record.status != EscalationStatus.PENDING.value:
        raise ExecutionError("Sözleşme artışı artık onay beklemiyor.")
    contract = await session.scalar(
        select(Contract).where(Contract.id == record.contract_id).with_for_update()
    )
    if contract is None or contract.is_archived:
        raise ExecutionError("Sözleşme arşivde; artış uygulanamaz.")
    now = request.decided_at or datetime.now(UTC)
    try:
        await _ensure_open(session, record.due_date, now)
    except FinanceRuleError as exc:
        raise ExecutionError(exc.message) from exc
    names = await _deciders(session, request, Decision.APPROVE)
    await _apply_escalation(
        session,
        contract,
        record,
        actor=Person(request.requested_by, request.requested_by_name),
        now=now,
    )
    record.decided_by = ", ".join(n for n, _ in names) or record.requested_by
    await session.flush()


async def _close_escalation(session: AsyncSession, request: ApprovalRequest) -> None:
    record = await _escalation_of_request(session, request)
    if record is None or record.status != EscalationStatus.PENDING.value:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        last = (await _deciders(session, request, Decision.REJECT))[-1:]
        record.status = EscalationStatus.REJECTED.value
        record.new_amount = record.old_amount
        record.decided_by = last[0][0] if last else None
        record.note = (last[0][1] if last else None) or record.note
        record.decided_at = request.decided_at
    else:
        record.status = EscalationStatus.WITHDRAWN.value
    await session.flush()


approvals_svc.register_executor(ActionType.PENALTY_DEDUCTION, _execute_penalty)
approvals_svc.register_closer(ActionType.PENALTY_DEDUCTION, _close_penalty)
approvals_svc.register_executor(ActionType.CONTRACT_ESCALATION, _execute_escalation)
approvals_svc.register_closer(ActionType.CONTRACT_ESCALATION, _close_escalation)


# --- Zamanlanmış iş (günde bir) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobResult:
    site_id: uuid.UUID
    drafts: int  # geçen ay için açılan karne taslağı
    escalations: int  # artış onayı / endeks eksik bildirimi
    renewals: int  # bitiş 60/30 gün hatırlatması


def _active_contracts() -> Select[Contract]:
    return select(Contract).where(Contract.is_archived.is_(False)).order_by(Contract.id)


async def run_site(session: AsyncSession, site: Site, *, today: date, now: datetime) -> JobResult:
    """Dönem sonu karne taslağı (geçen ay, KPI'lı sözleşmeler), artış 30 gün kala onay görevi ya
    da "endeks eksik" uyarısı, bitişe 60/30 gün kala yenileme hatırlatması. Tekrar güvenli."""
    managers = await permission_holders(session, site, Permission.CONTRACTS_MANAGE, now)
    last_month = YearMonth.of(today).previous()
    key = f"{last_month.year:04d}-{last_month.month:02d}"
    with_kpis = set(await session.scalars(select(ContractKpi.contract_id).distinct()))
    drafts = escalations = renewals = 0
    for contract in await session.scalars(_active_contracts()):
        if (
            contract.id in with_kpis
            and contract.start_date <= last_month.last_day
            and contract.end_date >= last_month.first_day
            and await get_card(session, contract.id, key) is None
        ):
            session.add(
                ContractScorecard(
                    id=uuid.uuid7(),
                    contract_id=contract.id,
                    period=key,
                    status=ScorecardStatus.DRAFT.value,
                    manual_values={},
                )
            )
            await session.flush()
            drafts += 1
            await _notify(
                session,
                managers,
                contract,
                "contract.scorecard_ready",
                f"{last_month.name} hizmet karnesi hazır: {contract.vendor}",
                "Değerleri kontrol edip karneyi kesinleştirin.",
                f"scorecard:{key}",
            )
        p = await preview(session, contract, today=today)
        if p.status is PreviewStatus.DUE and p.date is not None:
            escalations += await _notify(
                session,
                managers,
                contract,
                "contract.escalation_due",
                f"Sözleşme artışı onay bekliyor: {contract.vendor}",
                f"{p.date:%d.%m.%Y} · {format_money_tr(p.old_amount or Decimal(0))} → "
                f"{format_money_tr(p.new_amount or Decimal(0))}",
                f"escalation:{p.date}",
            )
        elif p.status is PreviewStatus.MISSING_INDEX and p.date is not None:
            escalations += await _notify(
                session,
                managers,
                contract,
                "contract.index_missing",
                f"Endeks değeri eksik: {contract.vendor}",
                f"{p.index_period} dönemi TÜFE/ÜFE girilmedi; platform yöneticisine bildirildi.",
                f"index:{p.date}",
            )
        left = (contract.end_date - today).days
        if 0 <= left <= 60:
            mark = "30" if left <= 30 else "60"
            renewals += await _notify(
                session,
                managers,
                contract,
                "contract.renewal",
                f"Sözleşme bitişine {left} gün: {contract.vendor}",
                "Son 12 ayın karnesine bakarak yenileyin, yeniden teklif alın ya da sonlandırın.",
                f"renewal:{contract.end_date}:{mark}",
            )
    return JobResult(site.id, drafts, escalations, renewals)


async def _notify(
    session: AsyncSession,
    users: set[uuid.UUID],
    contract: Contract,
    kind: str,
    title: str,
    body: str,
    key: str,
) -> int:
    return await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=body,
            link_entity="contracts",
            link_id=contract.id,
        ),
        dedupe_key=f"contract:{contract.id}:{key}",
    )


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[JobResult]:
    with all_sites_scope():
        async with factory() as session:
            site_ids = sorted(
                set(
                    await session.scalars(
                        select(Contract.site_id).where(Contract.is_archived.is_(False)).distinct()
                    )
                )
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append(await run_site(session, site, today=today, now=now))
    return results


@dataclass(frozen=True, slots=True)
class MissingIndex:
    period: str
    contracts: int
    sites: int


async def missing_indexes(
    factory: async_sessionmaker[AsyncSession], *, today: date
) -> list[MissingIndex]:
    """Platform uyarısı: 30 gün içinde artışı olan ve endeks dönemi girilmemiş sözleşmeler."""
    with all_sites_scope():
        async with factory() as session:
            rows = list(
                await session.execute(
                    select(
                        Contract.site_id,
                        Contract.start_date,
                        Contract.end_date,
                        Contract.escalation_period,
                    ).where(
                        Contract.is_archived.is_(False),
                        Contract.amount.is_not(None),
                        Contract.escalation_kind.in_(
                            [
                                EscalationKind.TUFE.value,
                                EscalationKind.UFE.value,
                                EscalationKind.AVG.value,
                            ]
                        ),
                    )
                )
            )
            needed: dict[str, tuple[int, set[uuid.UUID]]] = {}
            for site_id, start, end, period in rows:
                day = escalation_date(start, end, EscalationPeriod(period), today)
                if day is None or (day - today).days > DUE_DAYS:
                    continue
                key = index_period(day)
                count, sites = needed.get(key, (0, set()))
                sites.add(site_id)
                needed[key] = (count + 1, sites)
            if not needed:
                return []
            present = set(
                await session.scalars(
                    select(IndexValue.period).where(IndexValue.period.in_(list(needed))).distinct()
                )
            )
    return [
        MissingIndex(period, count, len(sites))
        for period, (count, sites) in sorted(needed.items())
        if period not in present
    ]
