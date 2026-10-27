"""Teknik varlık, yasal muayene takvimi ve periyodik bakım — Yazılım Dokümanı HIZ-07, HIZ-06;
PM kararı K28 (mali demirbaş `/assets` ayrı kalır).

Çekirdek (modül yok). Okuma `inventory.read` (yönetici, yönetim kurulu, muhasebe — maliyet,
teknik ekip), yazma `inventory.manage` (yönetici, teknik ekip). İş emri = talep modülündeki talep
(modül kapalıysa iş emri kaydı açılır ama talep açılmaz). Muayene raporu zorunlu; belge arşivine
bağlanır (`/subject-documents/{id}/download`).
"""

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.expenses import StoreDep
from site_yonetim.api.v1.ops_shared import fields_error, ops_error
from site_yonetim.api.v1.subject_documents import LinkedDocumentOut
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.documents import DocumentRuleError
from site_yonetim.domain.facility import (
    INSPECTION_TEMPLATES,
    DueWarning,
    InspectionResult,
    MaintenanceRunStatus,
    OpsError,
    PeriodUnit,
    TechnicalAssetCategory,
    TechnicalAssetStatus,
    due_warning,
)
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import (
    AssetInspectionRecord,
    AssetInspectionSchedule,
    MaintenancePlan,
    MaintenanceRun,
    Request,
    TechnicalAsset,
)
from site_yonetim.services import documents as documents_svc
from site_yonetim.services import subject_documents as docs_svc
from site_yonetim.services import technical_assets as svc

router = APIRouter(prefix="/sites/{slug}", tags=["teknik varlık ve bakım"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.INVENTORY_READ))]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.INVENTORY_MANAGE))]
LIST_MAX = 1000


def _requests_on(ctx: SiteContext) -> bool:
    return ModuleKey.REQUESTS in ctx.modules


# --- Şablonlar ----------------------------------------------------------------------


class InspectionTemplateOut(BaseModel):
    key: str
    name: str
    categories: list[TechnicalAssetCategory]
    period_months: int = Field(description="önerilen periyot (site değiştirir)")


@router.get(
    "/asset-inspection-templates", summary="Yasal muayene şablonları (kategoriye göre öneri)"
)
async def inspection_templates(ctx: Reader) -> list[InspectionTemplateOut]:
    return [
        InspectionTemplateOut(
            key=t.key, name=t.name, categories=list(t.categories), period_months=t.period_months
        )
        for t in INSPECTION_TEMPLATES
    ]


# --- Varlık kartı -------------------------------------------------------------------


class InspectionScheduleIn(BaseModel):
    template_key: str | None = Field(default=None, max_length=40)
    name: str | None = Field(default=None, max_length=200, description="boş: şablon adı")
    period_months: int | None = Field(default=None, description="1–120; boş: şablon periyodu")
    last_inspected_on: dt.date | None = None
    next_due_on: dt.date | None = Field(default=None, description="son muayene bilinmiyorsa")
    is_active: bool | None = None


class TechnicalAssetIn(BaseModel):
    category: str | None = Field(default=None, max_length=30)
    name: str | None = Field(default=None, max_length=400)
    location: str | None = Field(default=None, max_length=400)
    brand: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    serial_no: str | None = Field(default=None, max_length=200)
    installed_on: dt.date | None = None
    warranty_until: dt.date | None = None
    contract_id: uuid.UUID | None = Field(default=None, description="bakım sözleşmesi")
    service_company: str | None = Field(default=None, max_length=400, description="sorumlu firma")
    status: str | None = Field(default=TechnicalAssetStatus.WORKING.value, max_length=20)
    note: str | None = Field(default=None, max_length=2000)
    inspections: list[InspectionScheduleIn] = Field(
        default_factory=list, max_length=20, description="yalnız eklemede: muayene takvimi"
    )


class InspectionScheduleOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    template_key: str | None
    name: str
    period_months: int
    last_inspected_on: dt.date | None
    next_due_on: dt.date
    days_left: int
    warning: DueWarning | None = Field(description='"60" · "30" · "7" · "overdue" · null')
    is_active: bool

    @classmethod
    def of(cls, s: AssetInspectionSchedule, today: dt.date) -> InspectionScheduleOut:
        left = (s.next_due_on - today).days
        return cls(
            id=s.id,
            asset_id=s.asset_id,
            template_key=s.template_key,
            name=s.name,
            period_months=s.period_months,
            last_inspected_on=s.last_inspected_on,
            next_due_on=s.next_due_on,
            days_left=left,
            warning=due_warning(left) if s.is_active else None,
            is_active=s.is_active,
        )


class TechnicalAssetOut(BaseModel):
    id: uuid.UUID
    code: str = Field(description="TV-0001 — sunucu verir, değişmez")
    category: TechnicalAssetCategory
    name: str
    location: str | None
    brand: str | None
    model: str | None
    serial_no: str | None
    installed_on: dt.date | None
    warranty_until: dt.date | None
    under_warranty: bool
    contract_id: uuid.UUID | None
    contract_name: str | None
    service_company: str | None
    status: TechnicalAssetStatus
    note: str | None
    next_inspection: InspectionScheduleOut | None = Field(description="en yakın aktif muayene")


def _asset_out(
    a: TechnicalAsset,
    today: dt.date,
    schedules: list[AssetInspectionSchedule],
    contracts: dict[uuid.UUID, str],
) -> TechnicalAssetOut:
    active = [s for s in schedules if s.is_active]
    upcoming = min(active, key=lambda s: s.next_due_on) if active else None
    return TechnicalAssetOut(
        id=a.id,
        code=a.code,
        category=TechnicalAssetCategory(a.category),
        name=a.name,
        location=a.location,
        brand=a.brand,
        model=a.model,
        serial_no=a.serial_no,
        installed_on=a.installed_on,
        warranty_until=a.warranty_until,
        under_warranty=a.warranty_until is not None and a.warranty_until >= today,
        contract_id=a.contract_id,
        contract_name=contracts.get(a.contract_id) if a.contract_id else None,
        service_company=a.service_company,
        status=TechnicalAssetStatus(a.status),
        note=a.note,
        next_inspection=InspectionScheduleOut.of(upcoming, today) if upcoming else None,
    )


async def _asset_outs(
    ctx: SiteContext, rows: list[TechnicalAsset], today: dt.date
) -> list[TechnicalAssetOut]:
    schedules = await svc.schedules_for(ctx.session, [a.id for a in rows])
    contracts = await svc.contract_names(
        ctx.session, {a.contract_id for a in rows if a.contract_id}
    )
    return [_asset_out(a, today, schedules.get(a.id, []), contracts) for a in rows]


@router.get(
    "/technical-assets", summary="Teknik varlıklar (koda göre; mali demirbaştan ayrı — K28)"
)
async def list_assets(
    ctx: Reader,
    today: TodayDep,
    category: TechnicalAssetCategory | None = None,
    status_: Annotated[TechnicalAssetStatus | None, Query(alias="status")] = None,
) -> list[TechnicalAssetOut]:
    rows = list(await ctx.session.scalars(svc.assets_query(category, status_).limit(LIST_MAX)))
    return await _asset_outs(ctx, rows, today)


def _asset_data(body: TechnicalAssetIn, current: svc.AssetData | None) -> svc.AssetData:
    keys = [k for k in TechnicalAssetIn.model_fields if k != "inspections"]
    values = {k: getattr(body, k) for k in keys}
    if current is None:
        return svc.AssetData(**values)
    sent = body.model_fields_set
    return svc.AssetData(**{k: values[k] if k in sent else getattr(current, k) for k in keys})


def _schedule_data(body: InspectionScheduleIn) -> svc.ScheduleData:
    return svc.ScheduleData(
        template_key=body.template_key,
        name=body.name,
        period_months=body.period_months,
        last_inspected_on=body.last_inspected_on,
        next_due_on=body.next_due_on,
        is_active=body.is_active,
    )


@router.post("/technical-assets", status_code=status.HTTP_201_CREATED, summary="Teknik varlık ekle")
async def create_asset(
    body: TechnicalAssetIn, ctx: Manager, today: TodayDep
) -> Written[TechnicalAssetOut]:
    try:
        asset = await svc.create_asset(
            ctx.session,
            _asset_data(body, None),
            [_schedule_data(s) for s in body.inspections],
            today=today,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _asset_outs(ctx, [asset], today))[0]
    await ctx.session.commit()
    return Written(data=out, message=f"{asset.code} {asset.name} eklendi.")


async def _asset(ctx: SiteContext, asset_id: uuid.UUID, *, lock: bool = False) -> TechnicalAsset:
    asset = await svc.get_asset(ctx.session, asset_id, lock=lock)
    if asset is None:
        raise NotFoundError("Varlık bulunamadı.")
    return asset


@router.patch(
    "/technical-assets/{asset_id}", summary="Varlık kartını düzenle (kısmi; kod değişmez)"
)
async def update_asset(
    asset_id: uuid.UUID, body: TechnicalAssetIn, ctx: Manager, today: TodayDep
) -> Written[TechnicalAssetOut]:
    asset = await _asset(ctx, asset_id, lock=True)
    try:
        await svc.update_asset(ctx.session, asset, _asset_data(body, svc.current_asset(asset)))
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _asset_outs(ctx, [asset], today))[0]
    await ctx.session.commit()
    return Written(data=out, message=f"{asset.code} {asset.name} güncellendi.")


class InspectionRecordOut(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID
    asset_id: uuid.UUID
    inspected_on: dt.date
    result: InspectionResult
    inspector: str | None
    note: str | None
    cost: Money | None
    report_document_id: uuid.UUID = Field(description="GET …/subject-documents/{id}/download")
    request_id: uuid.UUID | None = Field(description="olumsuz sonuçta açılan düzeltme iş emri")
    by: str
    at: dt.datetime

    @classmethod
    def of(cls, r: AssetInspectionRecord) -> InspectionRecordOut:
        return cls(
            id=r.id,
            schedule_id=r.schedule_id,
            asset_id=r.asset_id,
            inspected_on=r.inspected_on,
            result=InspectionResult(r.result),
            inspector=r.inspector,
            note=r.note,
            cost=r.cost,
            report_document_id=r.report_document_id,
            request_id=r.request_id,
            by=r.by_name,
            at=r.created_at,
        )


class MaintenancePlanOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    asset_code: str
    asset_name: str
    title: str
    description: str | None
    period_unit: PeriodUnit
    period_count: int
    next_due_on: dt.date
    days_left: int
    responsible: str | None
    contract_id: uuid.UUID | None
    checklist_template_id: uuid.UUID | None
    is_active: bool
    open_run_id: uuid.UUID | None = Field(description="açık iş emri (bakım bekleniyor)")


class TechnicalAssetDetailOut(BaseModel):
    asset: TechnicalAssetOut
    inspections: list[InspectionScheduleOut]
    records: list[InspectionRecordOut] = Field(description="muayene geçmişi (yeniden eskiye)")
    plans: list[MaintenancePlanOut]
    documents: list[LinkedDocumentOut]


async def _plan_outs(
    ctx: SiteContext, plans: list[MaintenancePlan], today: dt.date
) -> list[MaintenancePlanOut]:
    assets = await svc.assets_by_id(ctx.session, {p.asset_id for p in plans})
    open_runs: dict[uuid.UUID, uuid.UUID] = {}
    if plans:
        for run in await ctx.session.scalars(
            svc.runs_query(status=MaintenanceRunStatus.OPEN).where(
                MaintenanceRun.plan_id.in_([p.id for p in plans])
            )
        ):
            open_runs.setdefault(run.plan_id, run.id)
    return [
        MaintenancePlanOut(
            id=p.id,
            asset_id=p.asset_id,
            asset_code=assets[p.asset_id].code if p.asset_id in assets else "—",
            asset_name=assets[p.asset_id].name if p.asset_id in assets else "—",
            title=p.title,
            description=p.description,
            period_unit=PeriodUnit(p.period_unit),
            period_count=p.period_count,
            next_due_on=p.next_due_on,
            days_left=(p.next_due_on - today).days,
            responsible=p.responsible,
            contract_id=p.contract_id,
            checklist_template_id=p.checklist_template_id,
            is_active=p.is_active,
            open_run_id=open_runs.get(p.id),
        )
        for p in plans
    ]


@router.get(
    "/technical-assets/{asset_id}", summary="Varlık kartı: muayene takvimi, geçmiş, bakım planları"
)
async def get_asset(asset_id: uuid.UUID, ctx: Reader, today: TodayDep) -> TechnicalAssetDetailOut:
    asset = await _asset(ctx, asset_id)
    out = (await _asset_outs(ctx, [asset], today))[0]
    schedules = (await svc.schedules_for(ctx.session, [asset.id]))[asset.id]
    plans = list(await ctx.session.scalars(svc.plans_query(asset.id)))
    docs = await docs_svc.for_subjects(ctx.session, "technical_assets", [asset.id])
    return TechnicalAssetDetailOut(
        asset=out,
        inspections=[InspectionScheduleOut.of(s, today) for s in schedules],
        records=[InspectionRecordOut.of(r) for r in await svc.records_for(ctx.session, asset.id)],
        plans=await _plan_outs(ctx, plans, today),
        documents=[LinkedDocumentOut.of(d) for d in docs[asset.id]],
    )


# --- Muayene takvimi ----------------------------------------------------------------


@router.post(
    "/technical-assets/{asset_id}/inspection-schedules",
    status_code=status.HTTP_201_CREATED,
    summary="Varlığa yasal muayene ekle (periyot + son muayene → sonraki tarih)",
)
async def add_schedule(
    asset_id: uuid.UUID, body: InspectionScheduleIn, ctx: Manager, today: TodayDep
) -> Written[InspectionScheduleOut]:
    asset = await _asset(ctx, asset_id)
    try:
        schedule = await svc.add_schedule(ctx.session, asset, _schedule_data(body), today=today)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = InspectionScheduleOut.of(schedule, today)
    await ctx.session.commit()
    return Written(
        data=out,
        message=(
            f"{schedule.name} takvime eklendi; sonraki muayene {schedule.next_due_on:%d.%m.%Y}."
        ),
    )


@router.patch(
    "/asset-inspection-schedules/{schedule_id}", summary="Muayene takvimini düzenle / kapat"
)
async def update_schedule(
    schedule_id: uuid.UUID, body: InspectionScheduleIn, ctx: Manager, today: TodayDep
) -> Written[InspectionScheduleOut]:
    schedule = await svc.get_schedule(ctx.session, schedule_id)
    if schedule is None:
        raise NotFoundError("Muayene takvimi bulunamadı.")
    sent = body.model_fields_set
    data = svc.ScheduleData(
        template_key=body.template_key if "template_key" in sent else schedule.template_key,
        name=body.name if "name" in sent else schedule.name,
        period_months=body.period_months if "period_months" in sent else schedule.period_months,
        last_inspected_on=(
            body.last_inspected_on if "last_inspected_on" in sent else schedule.last_inspected_on
        ),
        next_due_on=body.next_due_on if "next_due_on" in sent else schedule.next_due_on,
        is_active=body.is_active,
    )
    try:
        await svc.update_schedule(ctx.session, schedule, data, today=today)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = InspectionScheduleOut.of(schedule, today)
    await ctx.session.commit()
    return Written(data=out, message="Muayene takvimi güncellendi.")


class InspectionCalendarRowOut(BaseModel):
    schedule: InspectionScheduleOut
    asset_id: uuid.UUID
    asset_code: str
    asset_name: str
    asset_category: TechnicalAssetCategory
    location: str | None
    service_company: str | None


@router.get("/asset-inspections/calendar", summary="Yasal muayene takvimi (yaklaşan önce)")
async def inspection_calendar(
    ctx: Reader,
    today: TodayDep,
    within_days: Annotated[
        int | None, Query(ge=1, le=3650, description="yalnız N gün içindekiler")
    ] = None,
) -> list[InspectionCalendarRowOut]:
    query = svc.calendar_query()
    if within_days is not None:
        query = query.where(
            AssetInspectionSchedule.next_due_on <= today + dt.timedelta(days=within_days)
        )
    schedules = list(await ctx.session.scalars(query.limit(LIST_MAX)))
    assets = await svc.assets_by_id(ctx.session, {s.asset_id for s in schedules})
    return [
        InspectionCalendarRowOut(
            schedule=InspectionScheduleOut.of(s, today),
            asset_id=a.id,
            asset_code=a.code,
            asset_name=a.name,
            asset_category=TechnicalAssetCategory(a.category),
            location=a.location,
            service_company=a.service_company,
        )
        for s in schedules
        if (a := assets.get(s.asset_id)) is not None
    ]


def _money(value: str | None, errors: dict[str, str]) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        errors["cost"] = "Bedeli 1234.56 biçiminde girin."
        return None
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        errors["cost"] = "Bedeli 1234.56 biçiminde girin."
        return None
    return amount


@router.post(
    "/asset-inspection-schedules/{schedule_id}/records",
    status_code=status.HTTP_201_CREATED,
    summary="Muayene sonucunu kaydet (multipart; rapor zorunlu) — olumsuzsa iş emri açılır",
)
async def record_inspection(
    schedule_id: uuid.UUID,
    ctx: Manager,
    current: CurrentUserDep,
    store: StoreDep,
    today: TodayDep,
    inspected_on: Annotated[dt.date | None, Form()] = None,
    result: Annotated[str | None, Form(max_length=10, description="pass · fail")] = None,
    inspector: Annotated[str | None, Form(max_length=200, description="muayene kuruluşu")] = None,
    note: Annotated[str | None, Form(max_length=1000)] = None,
    cost: Annotated[str | None, Form(max_length=30, description='"1500.00"')] = None,
    report: Annotated[
        UploadFile | None, File(description="rapor: PDF, PNG, JPEG, WebP · ≤ 10 MB")
    ] = None,
) -> Written[InspectionRecordOut]:
    schedule = await svc.get_schedule(ctx.session, schedule_id)
    if schedule is None:
        raise NotFoundError("Muayene takvimi bulunamadı.")
    asset = await _asset(ctx, schedule.asset_id, lock=True)
    errors: dict[str, str] = {}
    amount = _money(cost, errors)
    if errors:
        raise fields_error(FormFieldError(errors))
    data = await report.read(MAX_FILE_BYTES + 1) if report is not None else b""
    written: list[str] = []
    try:
        outcome = await svc.record_inspection(
            ctx.session,
            store,
            schedule,
            asset,
            svc.InspectionData(
                inspected_on=inspected_on,
                result=result,
                inspector=inspector,
                note=note,
                cost=amount,
                file_name=report.filename if report is not None else None,
                data=data,
            ),
            uploader=documents_svc.Uploader(current.user.id, current.user.full_name, False),
            requests_enabled=_requests_on(ctx),
            today=today,
        )
        written = outcome.written
        await ctx.session.refresh(outcome.record, ["created_at"])
        out = InspectionRecordOut.of(outcome.record)
        await ctx.session.commit()
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    except OpsError as exc:
        raise ops_error(exc) from exc
    except DocumentRuleError as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    except BaseException:
        for path in written:
            store.delete(path)
        raise
    if outcome.record.result == InspectionResult.FAIL.value:
        message = "Muayene olumsuz kaydedildi; varlık uygunsuz işaretlendi."
        if outcome.request is not None:
            message += f" Düzeltme iş emri açıldı (#{outcome.request.number})."
    else:
        message = f"Muayene kaydedildi; sonraki muayene {schedule.next_due_on:%d.%m.%Y}."
    return Written(data=out, message=message)


# --- Periyodik bakım ----------------------------------------------------------------


class MaintenancePlanIn(BaseModel):
    asset_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=400)
    description: str | None = Field(default=None, max_length=2000)
    period_unit: str | None = Field(
        default=PeriodUnit.MONTH.value, max_length=10, description="week · month"
    )
    period_count: int | None = Field(default=1, description="1–60")
    next_due_on: dt.date | None = Field(default=None, description="ilk / sıradaki bakım tarihi")
    responsible: str | None = Field(default=None, max_length=400, description="ekip ya da firma")
    contract_id: uuid.UUID | None = None
    checklist_template_id: uuid.UUID | None = None
    is_active: bool | None = None


@router.get("/maintenance-plans", summary="Periyodik bakım planları (sıradaki tarihe göre)")
async def list_plans(
    ctx: Reader, today: TodayDep, asset_id: uuid.UUID | None = None
) -> list[MaintenancePlanOut]:
    plans = list(await ctx.session.scalars(svc.plans_query(asset_id).limit(LIST_MAX)))
    return await _plan_outs(ctx, plans, today)


def _plan_data(body: MaintenancePlanIn, current: svc.PlanData | None) -> svc.PlanData:
    values = {k: getattr(body, k) for k in MaintenancePlanIn.model_fields}
    if current is None:
        return svc.PlanData(**values)
    sent = body.model_fields_set
    return svc.PlanData(**{k: values[k] if k in sent else getattr(current, k) for k in values})


@router.post("/maintenance-plans", status_code=status.HTTP_201_CREATED, summary="Bakım planı ekle")
async def create_plan(
    body: MaintenancePlanIn, ctx: Manager, today: TodayDep
) -> Written[MaintenancePlanOut]:
    try:
        plan = await svc.save_plan(ctx.session, _plan_data(body, None), None)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _plan_outs(ctx, [plan], today))[0]
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"Bakım planı eklendi; ilk iş emri {plan.next_due_on:%d.%m.%Y} tarihinde açılır.",
    )


@router.patch("/maintenance-plans/{plan_id}", summary="Bakım planını düzenle / kapat")
async def update_plan(
    plan_id: uuid.UUID, body: MaintenancePlanIn, ctx: Manager, today: TodayDep
) -> Written[MaintenancePlanOut]:
    plan = await svc.get_plan(ctx.session, plan_id)
    if plan is None:
        raise NotFoundError("Bakım planı bulunamadı.")
    try:
        await svc.save_plan(ctx.session, _plan_data(body, svc.current_plan(plan)), plan)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = (await _plan_outs(ctx, [plan], today))[0]
    await ctx.session.commit()
    return Written(data=out, message="Bakım planı güncellendi.")


class MaintenanceRunOut(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    plan_title: str
    asset_id: uuid.UUID
    asset_code: str
    asset_name: str
    due_on: dt.date
    status: MaintenanceRunStatus
    request_id: uuid.UUID | None = Field(description="iş emri (talep)")
    request_number: int | None
    done_on: dt.date | None
    cost: Money | None
    note: str | None
    issue_request_id: uuid.UUID | None = Field(description="bakımda bulunan arızanın iş emri")
    done_by: str | None


async def _run_outs(ctx: SiteContext, runs: list[MaintenanceRun]) -> list[MaintenanceRunOut]:
    assets = await svc.assets_by_id(ctx.session, {r.asset_id for r in runs})
    plan_ids = {r.plan_id for r in runs}
    plans = (
        {
            p.id: p
            for p in await ctx.session.scalars(
                svc.plans_query().where(MaintenancePlan.id.in_(plan_ids))
            )
        }
        if plan_ids
        else {}
    )
    request_ids = {r.request_id for r in runs if r.request_id}
    numbers: dict[uuid.UUID, int] = {}
    if request_ids:
        rows = await ctx.session.execute(
            select(Request.id, Request.number).where(Request.id.in_(request_ids))
        )
        numbers = {row[0]: row[1] for row in rows}
    return [
        MaintenanceRunOut(
            id=r.id,
            plan_id=r.plan_id,
            plan_title=plans[r.plan_id].title if r.plan_id in plans else "—",
            asset_id=r.asset_id,
            asset_code=assets[r.asset_id].code if r.asset_id in assets else "—",
            asset_name=assets[r.asset_id].name if r.asset_id in assets else "—",
            due_on=r.due_on,
            status=MaintenanceRunStatus(r.status),
            request_id=r.request_id,
            request_number=numbers.get(r.request_id) if r.request_id else None,
            done_on=r.done_on,
            cost=r.cost,
            note=r.note,
            issue_request_id=r.issue_request_id,
            done_by=r.done_by_name,
        )
        for r in runs
    ]


@router.get("/maintenance-runs", summary="Bakım iş emirleri (yeniden eskiye)")
async def list_runs(
    ctx: Reader,
    asset_id: uuid.UUID | None = None,
    status_: Annotated[MaintenanceRunStatus | None, Query(alias="status")] = None,
) -> list[MaintenanceRunOut]:
    runs = list(
        await ctx.session.scalars(svc.runs_query(asset_id=asset_id, status=status_).limit(LIST_MAX))
    )
    return await _run_outs(ctx, runs)


@router.post(
    "/maintenance-plans/{plan_id}/runs",
    status_code=status.HTTP_201_CREATED,
    summary="Sıradaki bakım için iş emrini şimdi aç (gece işini beklemeden)",
)
async def open_run_now(
    plan_id: uuid.UUID, ctx: Manager, current: CurrentUserDep
) -> Written[MaintenanceRunOut]:
    plan = await svc.get_plan(ctx.session, plan_id)
    if plan is None:
        raise NotFoundError("Bakım planı bulunamadı.")
    try:
        run = await svc.open_run(
            ctx.session, plan, requests_enabled=_requests_on(ctx), by=current.user.full_name
        )
    except OpsError as exc:
        raise ops_error(exc) from exc
    out = (await _run_outs(ctx, [run]))[0]
    await ctx.session.commit()
    return Written(data=out, message="Bakım iş emri açıldı.")


class RunCompleteIn(BaseModel):
    status: str | None = Field(
        default=MaintenanceRunStatus.DONE.value, max_length=10, description="done · skipped"
    )
    done_on: dt.date | None = None
    cost: Money | None = None
    note: str | None = Field(default=None, max_length=2000)
    issue_found: bool = False
    issue_description: str | None = Field(default=None, max_length=1000)


@router.post(
    "/maintenance-runs/{run_id}/complete",
    summary="Bakımı kapat — geçmişe yazılır, sonraki tarih hesaplanır; sorun varsa arıza iş emri",
)
async def complete_run(
    run_id: uuid.UUID, body: RunCompleteIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[MaintenanceRunOut]:
    run = await svc.get_run(ctx.session, run_id)
    if run is None:
        raise NotFoundError("Bakım iş emri bulunamadı.")
    try:
        issue = await svc.finish_run(
            ctx.session,
            run,
            svc.RunCompletion(
                status=body.status,
                done_on=body.done_on,
                cost=body.cost,
                note=body.note,
                issue_found=body.issue_found,
                issue_description=body.issue_description,
            ),
            requests_enabled=_requests_on(ctx),
            by=current.user.full_name,
            today=today,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    except OpsError as exc:
        raise ops_error(exc) from exc
    out = (await _run_outs(ctx, [run]))[0]
    plan = await svc.get_plan(ctx.session, run.plan_id)
    await ctx.session.commit()
    message = "Bakım kaydedildi"
    if plan is not None:
        message += f"; sonraki bakım {plan.next_due_on:%d.%m.%Y}"
    message += "."
    if issue is not None:
        message += f" Arıza iş emri açıldı (#{issue.number})."
    return Written(data=out, message=message)


# --- Zaman çizelgesi ----------------------------------------------------------------


class AssetTimelineEventOut(BaseModel):
    date: dt.date
    at: dt.datetime | None
    kind: str = Field(
        description="created · inspection_pass · inspection_fail · maintenance_open · "
        "maintenance_done · maintenance_skipped · document"
    )
    title: str
    detail: str | None
    cost: Money | None
    request_id: uuid.UUID | None
    request_number: int | None
    request_status: str | None
    document_id: uuid.UUID | None


class AssetAnnualCostOut(BaseModel):
    year: int
    total: Money = Field(description="muayene + bakım bedeli")


class AssetTimelineOut(BaseModel):
    events: list[AssetTimelineEventOut] = Field(description="yeniden eskiye")
    annual_costs: list[AssetAnnualCostOut]


@router.get(
    "/technical-assets/{asset_id}/timeline", summary="Varlık zaman çizelgesi ve yıllık maliyet"
)
async def asset_timeline(asset_id: uuid.UUID, ctx: Reader) -> AssetTimelineOut:
    asset = await _asset(ctx, asset_id)
    found = await svc.timeline(ctx.session, asset)
    return AssetTimelineOut(
        events=[
            AssetTimelineEventOut(
                date=e.on,
                at=e.at,
                kind=e.kind,
                title=e.title,
                detail=e.detail,
                cost=e.cost,
                request_id=e.request_id,
                request_number=found.requests[e.request_id].number
                if e.request_id in found.requests
                else None,
                request_status=found.requests[e.request_id].status
                if e.request_id in found.requests
                else None,
                document_id=e.document_id,
            )
            for e in found.events
        ],
        annual_costs=[AssetAnnualCostOut(year=y, total=t) for y, t in found.annual_costs.items()],
    )
