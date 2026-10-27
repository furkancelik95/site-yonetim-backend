"""Teknik varlık, yasal muayene takvimi ve periyodik bakım — Yazılım Dokümanı HIZ-07, HIZ-06;
PM kararı **K28** (mali demirbaş `assets` ile ayrı liste). Açık site kapsamında; transaction'ı
çağıran yönetir.

- Varlık kartı: kod `TV-0001` (sıralı, işlem kilidi altında), kategori, konum, marka/model, seri no,
  kurulum, garanti, bakım sözleşmesi (`contracts`), sorumlu firma, durum. Silinmez.
- Yasal muayene takvimi: kategoriye göre hazır şablon önerilir; periyot + son muayene → sonraki
  tarih (saklanır). Günlük iş 60/30/7 gün kala ve geçince yöneticilere (envanter yönetimi izni)
  bildirir — her eşik bir kez. Sorumlu firmaya dış bildirim K5 (SMS/e-posta) ile gelir.
- Muayene kaydında **rapor belgesi zorunlu** (belge arşivine bağlanır). Olumsuz sonuç düzeltme iş
  emri (talep) açar ve varlığı "uygunsuz" işaretler; sonraki tarih yeniden hesaplanır.
- Periyodik bakım planı: varlık, iş tanımı, periyot (hafta/ay), sorumlu, kontrol listesi. Günlük iş
  planlanan tarihte iş emri (talep) açar; bakım bitince geçmişe yazılır, sorun bulunduysa arıza iş
  emri açılır, sonraki tarih hesaplanır.
- Zaman çizelgesi: muayeneler, bakımlar, iş emirleri ve belgeler tek listede; yıllık maliyet
  (muayene + bakım bedeli). Malzeme (stok) maliyeti iş emri–stok bağı gelince eklenecek.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.documents import DocumentFieldError, DocumentKind
from site_yonetim.domain.facility import (
    PERIOD_MONTHS_MAX,
    TEMPLATE_BY_KEY,
    DueWarning,
    InspectionResult,
    MaintenanceRunStatus,
    OpsError,
    PeriodUnit,
    TechnicalAssetCategory,
    TechnicalAssetStatus,
    add_months,
    clean,
    conflict,
    due_warning,
    next_maintenance,
    request_category_for,
    technical_asset_code,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import RequestPriority
from site_yonetim.models import (
    AssetInspectionRecord,
    AssetInspectionSchedule,
    ChecklistTemplate,
    Contract,
    MaintenancePlan,
    MaintenanceRun,
    Request,
    Site,
    TechnicalAsset,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.models.operations import TITLE_MAX
from site_yonetim.services import documents as documents_svc
from site_yonetim.services import notifications, subject_documents
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.facility_common import holders
from site_yonetim.services.files import FileStore
from site_yonetim.services.sites import available_modules

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
JOB_ACTOR = "Teknik varlık işi"
OVERDUE_NOTICE_DAYS = 30
WARN_WINDOW = 60
INSPECTION_KIND = {
    TechnicalAssetCategory.ELEVATOR: DocumentKind.ELEVATOR,
    TechnicalAssetCategory.FIRE_SYSTEM: DocumentKind.FIRE_REPORT,
}


# --- Varlık kartı -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetData:
    category: str | None
    name: str | None
    location: str | None
    brand: str | None
    model: str | None
    serial_no: str | None
    installed_on: date | None
    warranty_until: date | None
    contract_id: uuid.UUID | None
    service_company: str | None
    status: str | None
    note: str | None


def current_asset(a: TechnicalAsset) -> AssetData:
    return AssetData(
        category=a.category,
        name=a.name,
        location=a.location,
        brand=a.brand,
        model=a.model,
        serial_no=a.serial_no,
        installed_on=a.installed_on,
        warranty_until=a.warranty_until,
        contract_id=a.contract_id,
        service_company=a.service_company,
        status=a.status,
        note=a.note,
    )


async def _check_asset(session: AsyncSession, data: AssetData) -> dict[str, str]:
    errors: dict[str, str] = {}
    if len(clean(data.name, 120)) < 2:
        errors["name"] = "Varlığın adını yazın (ör. A Blok asansörü)."
    if data.category not in {c.value for c in TechnicalAssetCategory}:
        errors["category"] = "Kategori seçin."
    if data.status not in {s.value for s in TechnicalAssetStatus}:
        errors["status"] = "Durumu seçin."
    if (
        data.installed_on is not None
        and data.warranty_until is not None
        and data.warranty_until < data.installed_on
    ):
        errors["warranty_until"] = "Garanti bitişi kurulumdan önce olamaz."
    if data.contract_id is not None and (
        await session.scalar(select(Contract.id).where(Contract.id == data.contract_id)) is None
    ):
        errors["contract_id"] = "Sözleşme bulunamadı."
    return errors


def _apply(asset: TechnicalAsset, data: AssetData) -> None:
    asset.category = str(data.category)
    asset.name = clean(data.name, 120)
    asset.location = clean(data.location, 120) or None
    asset.brand = clean(data.brand, 80) or None
    asset.model = clean(data.model, 80) or None
    asset.serial_no = clean(data.serial_no, 80) or None
    asset.installed_on = data.installed_on
    asset.warranty_until = data.warranty_until
    asset.contract_id = data.contract_id
    asset.service_company = clean(data.service_company, 120) or None
    asset.status = str(data.status)
    asset.note = clean(data.note, 1000) or None


@dataclass(frozen=True, slots=True)
class ScheduleData:
    template_key: str | None
    name: str | None
    period_months: int | None
    last_inspected_on: date | None
    next_due_on: date | None
    is_active: bool | None = None


async def create_asset(
    session: AsyncSession, data: AssetData, schedules: list[ScheduleData], *, today: date
) -> TechnicalAsset:
    errors = await _check_asset(session, data)
    checked: list[tuple[ScheduleData, date]] = []
    for index, schedule in enumerate(schedules):
        try:
            checked.append(_check_schedule(schedule, today))
        except FormFieldError as exc:
            errors.update({f"inspections.{index}.{k}": v for k, v in exc.fields.items()})
    if errors:
        raise FormFieldError(errors)
    scope = current_scope()
    await session.execute(
        _NUMBER_LOCK, {"key": f"technical_assets:{scope.site_id if scope else ''}"}
    )
    sequence = ((await session.scalar(select(func.max(TechnicalAsset.sequence)))) or 0) + 1
    asset = TechnicalAsset(id=uuid.uuid7(), sequence=sequence, code=technical_asset_code(sequence))
    _apply(asset, data)
    session.add(asset)
    await session.flush()
    for schedule, next_due in checked:
        _new_schedule(session, asset, schedule, next_due)
    await session.flush()
    return asset


async def update_asset(session: AsyncSession, asset: TechnicalAsset, data: AssetData) -> None:
    errors = await _check_asset(session, data)
    if errors:
        raise FormFieldError(errors)
    _apply(asset, data)
    await session.flush()


async def get_asset(
    session: AsyncSession, asset_id: uuid.UUID, *, lock: bool = False
) -> TechnicalAsset | None:
    query = select(TechnicalAsset).where(TechnicalAsset.id == asset_id)
    found: TechnicalAsset | None = await session.scalar(query.with_for_update() if lock else query)
    return found


def assets_query(
    category: TechnicalAssetCategory | None, status: TechnicalAssetStatus | None
) -> Select[TechnicalAsset]:
    query = select(TechnicalAsset)
    if category is not None:
        query = query.where(TechnicalAsset.category == category.value)
    if status is not None:
        query = query.where(TechnicalAsset.status == status.value)
    return query.order_by(TechnicalAsset.sequence)


async def contract_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Contract.id, Contract.vendor, Contract.subject).where(Contract.id.in_(ids))
    )
    return {row[0]: f"{row[1]} — {row[2]}" for row in rows}


# --- Yasal muayene takvimi ----------------------------------------------------------


def _check_schedule(data: ScheduleData, today: date) -> tuple[ScheduleData, date]:
    errors: dict[str, str] = {}
    template = TEMPLATE_BY_KEY.get(data.template_key or "")
    if data.template_key and template is None:
        errors["template_key"] = "Muayene şablonunu listeden seçin."
    name = clean(data.name, 120) or (template.name if template else "")
    if len(name) < 3:
        errors["name"] = "Muayenenin adını yazın."
    period = data.period_months or (template.period_months if template else None)
    if period is None or not 1 <= period <= PERIOD_MONTHS_MAX:
        errors["period_months"] = f"Periyot 1–{PERIOD_MONTHS_MAX} ay olmalı."
    if data.last_inspected_on is not None and data.last_inspected_on > today:
        errors["last_inspected_on"] = "Son muayene tarihi bugünden sonra olamaz."
    if data.last_inspected_on is None and data.next_due_on is None:
        errors["last_inspected_on"] = "Son muayene ya da sonraki muayene tarihini girin."
    if errors or period is None:
        raise FormFieldError(errors)
    next_due = (
        add_months(data.last_inspected_on, period)
        if data.last_inspected_on is not None
        else data.next_due_on or today
    )
    return ScheduleData(data.template_key, name, period, data.last_inspected_on, None), next_due


def _new_schedule(
    session: AsyncSession, asset: TechnicalAsset, data: ScheduleData, next_due: date
) -> AssetInspectionSchedule:
    schedule = AssetInspectionSchedule(
        id=uuid.uuid7(),
        asset_id=asset.id,
        template_key=data.template_key or None,
        name=str(data.name),
        period_months=int(data.period_months or 12),
        last_inspected_on=data.last_inspected_on,
        next_due_on=next_due,
        is_active=True,
    )
    session.add(schedule)
    return schedule


async def add_schedule(
    session: AsyncSession, asset: TechnicalAsset, data: ScheduleData, *, today: date
) -> AssetInspectionSchedule:
    checked, next_due = _check_schedule(data, today)
    schedule = _new_schedule(session, asset, checked, next_due)
    await session.flush()
    return schedule


async def update_schedule(
    session: AsyncSession, schedule: AssetInspectionSchedule, data: ScheduleData, *, today: date
) -> None:
    checked, next_due = _check_schedule(data, today)
    schedule.template_key = checked.template_key or None
    schedule.name = str(checked.name)
    schedule.period_months = int(checked.period_months or schedule.period_months)
    schedule.last_inspected_on = checked.last_inspected_on
    schedule.next_due_on = next_due
    if data.is_active is not None:
        schedule.is_active = data.is_active
    await session.flush()


async def get_schedule(
    session: AsyncSession, schedule_id: uuid.UUID
) -> AssetInspectionSchedule | None:
    found: AssetInspectionSchedule | None = await session.scalar(
        select(AssetInspectionSchedule)
        .where(AssetInspectionSchedule.id == schedule_id)
        .with_for_update()
    )
    return found


async def schedules_for(
    session: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[AssetInspectionSchedule]]:
    out: dict[uuid.UUID, list[AssetInspectionSchedule]] = {i: [] for i in asset_ids}
    if asset_ids:
        for s in await session.scalars(
            select(AssetInspectionSchedule)
            .where(AssetInspectionSchedule.asset_id.in_(asset_ids))
            .order_by(AssetInspectionSchedule.next_due_on)
        ):
            out[s.asset_id].append(s)
    return out


def calendar_query() -> Select[AssetInspectionSchedule]:
    return (
        select(AssetInspectionSchedule)
        .where(AssetInspectionSchedule.is_active)
        .order_by(AssetInspectionSchedule.next_due_on, AssetInspectionSchedule.id)
    )


async def assets_by_id(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, TechnicalAsset]:
    if not ids:
        return {}
    return {
        a.id: a
        for a in await session.scalars(select(TechnicalAsset).where(TechnicalAsset.id.in_(ids)))
    }


async def _open_request(
    session: AsyncSession,
    asset: TechnicalAsset,
    *,
    title: str,
    description: str,
    priority: RequestPriority,
    by: str,
) -> Request:
    return await requests_svc.create(
        session,
        requests_svc.NewRequest(
            title=title[:TITLE_MAX],
            description=description,
            category=request_category_for(TechnicalAssetCategory(asset.category)),
            priority=priority,
            unit_id=None,
            location=asset.location,
            reported_by_person_id=None,
        ),
        actor=by,
        now=datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class InspectionData:
    inspected_on: date | None
    result: str | None
    inspector: str | None
    note: str | None
    cost: Decimal | None
    file_name: str | None
    data: bytes


@dataclass
class RecordResult:
    record: AssetInspectionRecord
    request: Request | None
    written: list[str] = field(default_factory=list)


async def record_inspection(
    session: AsyncSession,
    store: FileStore,
    schedule: AssetInspectionSchedule,
    asset: TechnicalAsset,
    data: InspectionData,
    *,
    uploader: documents_svc.Uploader,
    requests_enabled: bool,
    today: date,
) -> RecordResult:
    """Rapor belgesi zorunlu; olumsuzsa düzeltme iş emri ve varlık "uygunsuz"."""
    errors: dict[str, str] = {}
    if data.inspected_on is None or data.inspected_on > today:
        errors["inspected_on"] = "Muayene tarihi bugün ya da önce olmalı."
    if data.result not in {r.value for r in InspectionResult}:
        errors["result"] = "Sonucu seçin (olumlu / olumsuz)."
    if data.cost is not None and data.cost < 0:
        errors["cost"] = "Bedel sıfır ya da daha büyük olmalı."
    if not data.data:
        errors["report"] = "Muayene raporunu yükleyin (zorunlu)."
    if errors or data.inspected_on is None:
        raise FormFieldError(errors)
    if not schedule.is_active:
        raise conflict("schedule_inactive", "Bu muayene takvimi kapalı.")
    record_id = uuid.uuid7()
    try:
        uploaded = await subject_documents.attach(
            session,
            store,
            subject_type="asset_inspection_records",
            subject_id=record_id,
            title=(
                f"{asset.code} {schedule.name} raporu {data.inspected_on:%d.%m.%Y} "
                f"#{record_id.hex[-6:]}"
            ),
            file_name=data.file_name,
            data=data.data,
            uploader=uploader,
            kind=INSPECTION_KIND.get(TechnicalAssetCategory(asset.category), DocumentKind.OTHER),
        )
    except DocumentFieldError as exc:
        raise FormFieldError({"report": next(iter(exc.fields.values()))}) from exc
    request: Request | None = None
    outcome = InspectionResult(str(data.result))
    if outcome is InspectionResult.FAIL:
        asset.status = TechnicalAssetStatus.NONCONFORMING.value
        if requests_enabled:
            request = await _open_request(
                session,
                asset,
                title=f"Muayene olumsuz: {asset.name} — {schedule.name}",
                description=(
                    f"{asset.code} {asset.name} için {data.inspected_on:%d.%m.%Y} tarihli "
                    f"{schedule.name} olumsuz. {clean(data.note, 500)}".strip()
                ),
                priority=RequestPriority.HIGH,
                by=uploader.name,
            )
    elif asset.status == TechnicalAssetStatus.NONCONFORMING.value:
        asset.status = TechnicalAssetStatus.WORKING.value
    record = AssetInspectionRecord(
        id=record_id,
        schedule_id=schedule.id,
        asset_id=asset.id,
        inspected_on=data.inspected_on,
        result=outcome.value,
        inspector=clean(data.inspector, 120) or None,
        note=clean(data.note, 1000) or None,
        cost=data.cost,
        report_document_id=uploaded.document.id,
        request_id=request.id if request else None,
        by_name=uploader.name,
    )
    session.add(record)
    if schedule.last_inspected_on is None or data.inspected_on >= schedule.last_inspected_on:
        schedule.last_inspected_on = data.inspected_on
        schedule.next_due_on = add_months(data.inspected_on, schedule.period_months)
    await session.flush()
    return RecordResult(record, request, [uploaded.storage_path])


async def records_for(session: AsyncSession, asset_id: uuid.UUID) -> list[AssetInspectionRecord]:
    return list(
        await session.scalars(
            select(AssetInspectionRecord)
            .where(AssetInspectionRecord.asset_id == asset_id)
            .order_by(AssetInspectionRecord.inspected_on.desc(), AssetInspectionRecord.id.desc())
        )
    )


# --- Muayene uyarısı (günlük iş) -----------------------------------------------------


def _due(today: date) -> Select[AssetInspectionSchedule]:
    return select(AssetInspectionSchedule).where(
        AssetInspectionSchedule.is_active,
        AssetInspectionSchedule.next_due_on <= today + timedelta(days=WARN_WINDOW),
        AssetInspectionSchedule.next_due_on >= today - timedelta(days=OVERDUE_NOTICE_DAYS),
    )


async def warn_inspections(session: AsyncSession, site: Site, *, today: date) -> int:
    schedules = list(await session.scalars(_due(today)))
    if not schedules:
        return 0
    assets = await assets_by_id(session, {s.asset_id for s in schedules})
    recipients = await holders(session, site, Permission.INVENTORY_MANAGE)
    written = 0
    for schedule in schedules:
        asset = assets.get(schedule.asset_id)
        if asset is None or asset.status == TechnicalAssetStatus.OUT_OF_SERVICE.value:
            continue
        left = (schedule.next_due_on - today).days
        warning = due_warning(left)
        if warning is None:
            continue
        title = (
            f"Muayene tarihi geçti: {asset.name} — {schedule.name}"
            if warning is DueWarning.OVERDUE
            else f"Muayeneye {left} gün kaldı: {asset.name} — {schedule.name}"
        )
        company = f" Sorumlu firma: {asset.service_company}." if asset.service_company else ""
        written += await notifications.notify(
            session,
            recipients,
            notifications.Message(
                category=NotificationCategory.OPERATIONS,
                kind="asset.inspection_overdue"
                if warning is DueWarning.OVERDUE
                else "asset.inspection_due",
                title=title,
                body=f"Muayene tarihi: {schedule.next_due_on:%d.%m.%Y}.{company}",
                link_entity="technical_assets",
                link_id=asset.id,
            ),
            dedupe_key=f"asset_inspection:{schedule.id}:{schedule.next_due_on}:{warning.value}",
        )
    return written


# --- Periyodik bakım ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanData:
    asset_id: uuid.UUID | None
    title: str | None
    description: str | None
    period_unit: str | None
    period_count: int | None
    next_due_on: date | None
    responsible: str | None
    contract_id: uuid.UUID | None
    checklist_template_id: uuid.UUID | None
    is_active: bool | None


def current_plan(p: MaintenancePlan) -> PlanData:
    return PlanData(
        asset_id=p.asset_id,
        title=p.title,
        description=p.description,
        period_unit=p.period_unit,
        period_count=p.period_count,
        next_due_on=p.next_due_on,
        responsible=p.responsible,
        contract_id=p.contract_id,
        checklist_template_id=p.checklist_template_id,
        is_active=p.is_active,
    )


async def save_plan(
    session: AsyncSession, data: PlanData, current: MaintenancePlan | None
) -> MaintenancePlan:
    errors: dict[str, str] = {}
    asset = await get_asset(session, data.asset_id) if data.asset_id else None
    if asset is None:
        errors["asset_id"] = "Varlık seçin."
    title = clean(data.title, 120)
    if len(title) < 3:
        errors["title"] = "Bakım işini yazın (ör. Aylık asansör bakımı)."
    if data.period_unit not in {u.value for u in PeriodUnit}:
        errors["period_unit"] = "Periyot birimini seçin (hafta / ay)."
    if data.period_count is None or not 1 <= data.period_count <= 60:
        errors["period_count"] = "Periyot 1–60 olmalı."
    if data.next_due_on is None:
        errors["next_due_on"] = "İlk bakım tarihini seçin."
    if data.contract_id is not None and (
        await session.scalar(select(Contract.id).where(Contract.id == data.contract_id)) is None
    ):
        errors["contract_id"] = "Sözleşme bulunamadı."
    if data.checklist_template_id is not None and (
        await session.scalar(
            select(ChecklistTemplate.id).where(ChecklistTemplate.id == data.checklist_template_id)
        )
        is None
    ):
        errors["checklist_template_id"] = "Denetim şablonu bulunamadı."
    if errors or asset is None or data.next_due_on is None or data.period_count is None:
        raise FormFieldError(errors)
    plan = current or MaintenancePlan(id=uuid.uuid7())
    plan.asset_id = asset.id
    plan.title = title
    plan.description = clean(data.description, 1000) or None
    plan.period_unit = str(data.period_unit)
    plan.period_count = data.period_count
    plan.next_due_on = data.next_due_on
    plan.responsible = clean(data.responsible, 120) or None
    plan.contract_id = data.contract_id
    plan.checklist_template_id = data.checklist_template_id
    plan.is_active = True if data.is_active is None else data.is_active
    if current is None:
        session.add(plan)
    await session.flush()
    return plan


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> MaintenancePlan | None:
    found: MaintenancePlan | None = await session.scalar(
        select(MaintenancePlan).where(MaintenancePlan.id == plan_id).with_for_update()
    )
    return found


def plans_query(asset_id: uuid.UUID | None = None) -> Select[MaintenancePlan]:
    query = select(MaintenancePlan)
    if asset_id is not None:
        query = query.where(MaintenancePlan.asset_id == asset_id)
    return query.order_by(MaintenancePlan.next_due_on, MaintenancePlan.id)


def runs_query(
    *, asset_id: uuid.UUID | None = None, status: MaintenanceRunStatus | None = None
) -> Select[MaintenanceRun]:
    query = select(MaintenanceRun)
    if asset_id is not None:
        query = query.where(MaintenanceRun.asset_id == asset_id)
    if status is not None:
        query = query.where(MaintenanceRun.status == status.value)
    return query.order_by(MaintenanceRun.due_on.desc(), MaintenanceRun.id.desc())


async def open_run(
    session: AsyncSession, plan: MaintenancePlan, *, requests_enabled: bool, by: str
) -> MaintenanceRun:
    """Planın sıradaki tarihi için iş emri (talep) açar; aynı dönem ikinci kez açılmaz."""
    if not plan.is_active:
        raise conflict("plan_inactive", "Bu bakım planı kapalı.")
    if await session.scalar(
        select(MaintenanceRun.id).where(
            MaintenanceRun.plan_id == plan.id, MaintenanceRun.due_on == plan.next_due_on
        )
    ):
        raise conflict("run_exists", "Bu dönemin iş emri zaten açılmış.")
    asset = await get_asset(session, plan.asset_id)
    if asset is None:  # pragma: no cover - FK
        raise OpsError(404, "not_found", "Varlık bulunamadı.")
    request = None
    if requests_enabled:
        description = f"{asset.code} {asset.name} — planlı bakım ({plan.next_due_on:%d.%m.%Y})."
        if plan.responsible:
            description += f" Sorumlu: {plan.responsible}."
        if plan.description:
            description += f" {plan.description}"
        request = await _open_request(
            session,
            asset,
            title=f"Periyodik bakım: {plan.title} — {asset.name}",
            description=description,
            priority=RequestPriority.NORMAL,
            by=by,
        )
    run = MaintenanceRun(
        id=uuid.uuid7(),
        plan_id=plan.id,
        asset_id=asset.id,
        due_on=plan.next_due_on,
        status=MaintenanceRunStatus.OPEN.value,
        request_id=request.id if request else None,
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> MaintenanceRun | None:
    found: MaintenanceRun | None = await session.scalar(
        select(MaintenanceRun).where(MaintenanceRun.id == run_id).with_for_update()
    )
    return found


@dataclass(frozen=True, slots=True)
class RunCompletion:
    status: str | None
    done_on: date | None
    cost: Decimal | None
    note: str | None
    issue_found: bool
    issue_description: str | None


async def finish_run(
    session: AsyncSession,
    run: MaintenanceRun,
    data: RunCompletion,
    *,
    requests_enabled: bool,
    by: str,
    today: date,
) -> Request | None:
    if run.status != MaintenanceRunStatus.OPEN.value:
        raise conflict("run_closed", "Bu bakım zaten kapatılmış.")
    errors: dict[str, str] = {}
    skipped = data.status == MaintenanceRunStatus.SKIPPED.value
    done_on = data.done_on or today
    if done_on > today:
        errors["done_on"] = "Bakım tarihi bugünden sonra olamaz."
    if data.cost is not None and data.cost < 0:
        errors["cost"] = "Bedel sıfır ya da daha büyük olmalı."
    note = clean(data.note, 1000) or None
    if skipped and (note is None or len(note) < 3):
        errors["note"] = "Bakımın neden yapılmadığını yazın."
    issue = clean(data.issue_description, 500)
    if data.issue_found and not skipped and len(issue) < 5:
        errors["issue_description"] = "Bulunan sorunu kısaca anlatın."
    if errors:
        raise FormFieldError(errors)
    plan = await get_plan(session, run.plan_id)
    asset = await get_asset(session, run.asset_id, lock=True)
    if plan is None or asset is None:  # pragma: no cover - FK
        raise OpsError(404, "not_found", "Bakım planı bulunamadı.")
    issue_request = None
    if data.issue_found and not skipped:
        asset.status = TechnicalAssetStatus.FAULTY.value
        if requests_enabled:
            issue_request = await _open_request(
                session,
                asset,
                title=f"Arıza (bakımda bulundu): {asset.name}",
                description=f"{plan.title} sırasında bulundu: {issue}",
                priority=RequestPriority.HIGH,
                by=by,
            )
    run.status = (MaintenanceRunStatus.SKIPPED if skipped else MaintenanceRunStatus.DONE).value
    run.done_on = done_on
    run.cost = None if skipped else data.cost
    run.note = note
    run.done_by_name = by
    run.issue_request_id = issue_request.id if issue_request else None
    if plan.next_due_on <= run.due_on or plan.next_due_on <= done_on:
        plan.next_due_on = next_maintenance(
            run.due_on, done_on, PeriodUnit(plan.period_unit), plan.period_count
        )
    await session.flush()
    return issue_request


async def generate_due(session: AsyncSession, site: Site, *, today: date) -> list[MaintenanceRun]:
    """Planlanan tarihi gelmiş (ve iş emri açılmamış) planlar için iş emri."""
    requests_enabled = ModuleKey.REQUESTS in await available_modules(session, site)
    plans = list(
        await session.scalars(
            select(MaintenancePlan)
            .where(MaintenancePlan.is_active, MaintenancePlan.next_due_on <= today)
            .with_for_update()
        )
    )
    created: list[MaintenanceRun] = []
    for plan in plans:
        exists = await session.scalar(
            select(MaintenanceRun.id).where(
                MaintenanceRun.plan_id == plan.id, MaintenanceRun.due_on == plan.next_due_on
            )
        )
        if exists:
            continue
        created.append(
            await open_run(session, plan, requests_enabled=requests_enabled, by=JOB_ACTOR)
        )
    if created:
        await notifications.notify(
            session,
            await holders(session, site, Permission.INVENTORY_MANAGE),
            notifications.Message(
                category=NotificationCategory.OPERATIONS,
                kind="maintenance.generated",
                title=f"{len(created)} periyodik bakım iş emri açıldı",
                link_entity="maintenance_runs",
            ),
            dedupe_key=f"maintenance.generated:{today}:{','.join(str(r.id) for r in created)}"[
                :300
            ],
        )
    return created


@dataclass(frozen=True, slots=True)
class JobResult:
    site_id: uuid.UUID
    warnings: int
    runs: int


async def run_daily(factory: async_sessionmaker[AsyncSession], *, today: date) -> list[JobResult]:
    """Günlük iş: muayene uyarıları (60/30/7) + planı gelen bakımların iş emri. Site başına ayrı
    transaction — bir sitedeki hata diğerini durdurmaz. Tekrar güvenli."""
    with all_sites_scope():
        async with factory() as session:
            site_ids: list[uuid.UUID] = sorted(
                set(
                    await session.scalars(
                        _due(today).with_only_columns(AssetInspectionSchedule.site_id)
                    )
                )
                | set(
                    await session.scalars(
                        select(MaintenancePlan.site_id).where(
                            MaintenancePlan.is_active, MaintenancePlan.next_due_on <= today
                        )
                    )
                )
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[JobResult] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                warnings = await warn_inspections(session, site, today=today)
                runs = await generate_due(session, site, today=today)
                results.append(JobResult(site_id, warnings, len(runs)))
    return results


# --- Zaman çizelgesi ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    on: date
    at: datetime | None
    kind: str
    title: str
    detail: str | None = None
    cost: Decimal | None = None
    request_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Timeline:
    events: list[TimelineEvent]
    annual_costs: dict[int, Decimal]
    requests: dict[uuid.UUID, Request]


async def timeline(session: AsyncSession, asset: TechnicalAsset) -> Timeline:
    events: list[TimelineEvent] = [
        TimelineEvent(
            asset.created_at.date(),
            asset.created_at,
            "created",
            f"Varlık kartı açıldı ({asset.code})",
        )
    ]
    schedules = {s.id: s for s in (await schedules_for(session, [asset.id]))[asset.id]}
    request_ids: set[uuid.UUID] = set()
    costs: dict[int, Decimal] = {}

    def add_cost(day: date, amount: Decimal | None) -> None:
        if amount is not None:
            costs[day.year] = costs.get(day.year, Decimal(0)) + amount

    for r in await records_for(session, asset.id):
        name = schedules[r.schedule_id].name if r.schedule_id in schedules else "Muayene"
        events.append(
            TimelineEvent(
                r.inspected_on,
                r.created_at,
                "inspection_pass" if r.result == InspectionResult.PASS.value else "inspection_fail",
                f"{name}: {'olumlu' if r.result == InspectionResult.PASS.value else 'olumsuz'}",
                detail=" · ".join(x for x in (r.inspector, r.note) if x) or None,
                cost=r.cost,
                request_id=r.request_id,
                document_id=r.report_document_id,
            )
        )
        add_cost(r.inspected_on, r.cost)
        if r.request_id:
            request_ids.add(r.request_id)
    plans = {p.id: p for p in await session.scalars(plans_query(asset.id))}
    for run in await session.scalars(runs_query(asset_id=asset.id)):
        plan = plans.get(run.plan_id)
        title = plan.title if plan else "Periyodik bakım"
        if run.status == MaintenanceRunStatus.OPEN.value:
            events.append(
                TimelineEvent(
                    run.due_on,
                    run.created_at,
                    "maintenance_open",
                    f"{title}: iş emri açıldı",
                    request_id=run.request_id,
                )
            )
        else:
            done = run.status == MaintenanceRunStatus.DONE.value
            events.append(
                TimelineEvent(
                    run.done_on or run.due_on,
                    run.updated_at,
                    "maintenance_done" if done else "maintenance_skipped",
                    f"{title}: {'yapıldı' if done else 'yapılmadı'}",
                    detail=run.note,
                    cost=run.cost,
                    request_id=run.issue_request_id or run.request_id,
                )
            )
            add_cost(run.done_on or run.due_on, run.cost)
        request_ids.update(x for x in (run.request_id, run.issue_request_id) if x)
    for document in (await subject_documents.for_subjects(session, "technical_assets", [asset.id]))[
        asset.id
    ]:
        events.append(
            TimelineEvent(
                document.created_at.date(),
                document.created_at,
                "document",
                f"Belge: {document.title}",
                document_id=document.id,
            )
        )
    requests: dict[uuid.UUID, Request] = {}
    if request_ids:
        requests = {
            r.id: r
            for r in await session.scalars(select(Request).where(Request.id.in_(request_ids)))
        }
    events.sort(
        key=lambda e: (e.on, e.at or datetime.min.replace(tzinfo=asset.created_at.tzinfo)),
        reverse=True,
    )
    return Timeline(events, dict(sorted(costs.items(), reverse=True)), requests)
