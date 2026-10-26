"""Devriye — servis isteği 46 (Yazılım Dokümanı GUV-09). Modül `visitors` (kapalıysa 404).

- Kurulum (nokta, plan yazma): `modules.manage`. Nokta ve plan okuma: kurulum ya da
  `security.incidents` (görevli turu yürütürken noktaları görür).
- Yürütme (başlat, okut, bitir) ve takvim/rapor: `security.incidents` (Yönetici de taşır).
- NFC okuma/eşleme ve çevrimdışı kuyruk mobil uygulamayla; web'de etiketteki kod girilir.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.gatehouse import access, rule_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.patrol import PatrolReport, PatrolTag, SlotStatus, check_plan, check_point
from site_yonetim.models import PatrolPlan, PatrolPoint, PatrolRun
from site_yonetim.services import patrol as svc

router = APIRouter(prefix="/sites/{slug}", tags=["devriye"])
V = (ModuleKey.VISITORS,)
Setup = Annotated[SiteContext, Depends(access(V, (Permission.MODULES_MANAGE,)))]
SetupRead = Annotated[
    SiteContext, Depends(access(V, (Permission.MODULES_MANAGE, Permission.SECURITY_INCIDENTS)))
]
Runner = Annotated[SiteContext, Depends(access(V, (Permission.SECURITY_INCIDENTS,)))]


# --- Noktalar ------------------------------------------------------------------------------


class PatrolPointOut(BaseModel):
    id: uuid.UUID
    name: str
    location: str | None
    area: str | None
    tag: PatrolTag
    code: str = Field(description="6 hane; QR içeriği SY-DEVRIYE:{kod}")
    is_active: bool


def _point(p: PatrolPoint) -> PatrolPointOut:
    return PatrolPointOut(
        id=p.id,
        name=p.name,
        location=p.location,
        area=p.area,
        tag=PatrolTag(p.tag),
        code=p.code,
        is_active=p.is_active,
    )


class PatrolPointIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    area: str | None = Field(default=None, max_length=200)
    tag: str | None = Field(default=None, description="qr · nfc · both")
    is_active: bool = True


@router.get("/patrol-points", summary="Kontrol noktaları")
async def list_points(ctx: SetupRead) -> list[PatrolPointOut]:
    return [_point(p) for p in await svc.points(ctx.session)]


async def _save_point(
    ctx: SiteContext, point: PatrolPoint | None, body: PatrolPointIn
) -> PatrolPointOut:
    try:
        form = check_point(
            name=body.name,
            location=body.location,
            area=body.area,
            tag=body.tag,
            taken=await svc.taken_names(ctx.session, point.id if point else None),
        )
    except FormFieldError as exc:
        raise rule_error(exc) from exc
    saved = await svc.save_point(ctx.session, point, form, is_active=body.is_active)
    await ctx.session.commit()
    return _point(saved)


@router.post("/patrol-points", status_code=status.HTTP_201_CREATED, summary="Nokta ekle")
async def create_point(ctx: Setup, body: PatrolPointIn) -> Written[PatrolPointOut]:
    out = await _save_point(ctx, None, body)
    return Written(data=out, message=f"{out.name} eklendi; etiket kodu {out.code}.")


@router.patch("/patrol-points/{point_id}", summary="Noktayı düzenle")
async def update_point(
    point_id: uuid.UUID, ctx: Setup, body: PatrolPointIn
) -> Written[PatrolPointOut]:
    point = await svc.get_point(ctx.session, point_id)
    if point is None:
        raise NotFoundError("Nokta bulunamadı.")
    out = await _save_point(ctx, point, body)
    return Written(data=out, message=f"{out.name} güncellendi.")


# --- Planlar --------------------------------------------------------------------------------


class PatrolPlanOut(BaseModel):
    id: uuid.UUID
    name: str
    point_ids: list[uuid.UUID] = Field(description="sırayla")
    ordered: bool
    start_times: list[str] = Field(description="SS:DD, sıralı")
    allowed_minutes: int
    assignee: str | None
    is_active: bool


def _plan(p: PatrolPlan) -> PatrolPlanOut:
    return PatrolPlanOut(
        id=p.id,
        name=p.name,
        point_ids=list(p.point_ids),
        ordered=p.ordered,
        start_times=list(p.start_times),
        allowed_minutes=p.allowed_minutes,
        assignee=p.assignee,
        is_active=p.is_active,
    )


class PatrolPlanIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    point_ids: list[str] = Field(default_factory=list, max_length=200)
    ordered: bool = True
    start_times: list[str] = Field(default_factory=list, max_length=48)
    allowed_minutes: int | None = None
    assignee: str | None = Field(default=None, max_length=200)
    is_active: bool = True


@router.get("/patrol-plans", summary="Tur planları")
async def list_plans(ctx: SetupRead) -> list[PatrolPlanOut]:
    return [_plan(p) for p in await svc.plans(ctx.session)]


async def _save_plan(
    ctx: SiteContext, plan: PatrolPlan | None, body: PatrolPlanIn
) -> PatrolPlanOut:
    try:
        form = check_plan(
            name=body.name,
            point_ids=body.point_ids,
            ordered=body.ordered,
            start_times=body.start_times,
            allowed_minutes=body.allowed_minutes,
            assignee=body.assignee,
            active_points=await svc.active_point_ids(ctx.session),
        )
    except FormFieldError as exc:
        raise rule_error(exc) from exc
    saved = await svc.save_plan(ctx.session, plan, form, is_active=body.is_active)
    await ctx.session.commit()
    return _plan(saved)


@router.post("/patrol-plans", status_code=status.HTTP_201_CREATED, summary="Tur planı ekle")
async def create_plan(ctx: Setup, body: PatrolPlanIn) -> Written[PatrolPlanOut]:
    out = await _save_plan(ctx, None, body)
    return Written(data=out, message=f"{out.name} turu eklendi.")


@router.patch("/patrol-plans/{plan_id}", summary="Tur planını düzenle")
async def update_plan(plan_id: uuid.UUID, ctx: Setup, body: PatrolPlanIn) -> Written[PatrolPlanOut]:
    plan = await svc.get_plan(ctx.session, plan_id)
    if plan is None:
        raise NotFoundError("Tur planı bulunamadı.")
    out = await _save_plan(ctx, plan, body)
    return Written(data=out, message=f"{out.name} güncellendi.")


# --- Rapor, takvim, yürütme -----------------------------------------------------------------


class PatrolReportOut(BaseModel):
    missed: list[str] = Field(description="okutulmayan nokta adları")
    out_of_order: list[str]
    duration_minutes: int
    overtime: bool
    late_start_minutes: int
    total_points: int
    scanned_points: int
    ok: bool


def _report(r: PatrolReport) -> PatrolReportOut:
    return PatrolReportOut(
        missed=r.missed,
        out_of_order=r.out_of_order,
        duration_minutes=r.duration_minutes,
        overtime=r.overtime,
        late_start_minutes=r.late_start_minutes,
        total_points=r.total_points,
        scanned_points=r.scanned_points,
        ok=r.ok,
    )


class PatrolScanOut(BaseModel):
    point_id: uuid.UUID
    at: dt.datetime
    note: str | None
    out_of_order: bool
    by: str


class PatrolRunOut(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    plan_name: str
    point_ids: list[uuid.UUID] = Field(description="başlatıldığı andaki plan noktaları")
    ordered: bool
    allowed_minutes: int
    slot: str | None
    started_by: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    scans: list[PatrolScanOut]
    report: PatrolReportOut


def _run(v: svc.RunView) -> PatrolRunOut:
    r = v.run
    return PatrolRunOut(
        id=r.id,
        plan_id=r.plan_id,
        plan_name=r.plan_name,
        point_ids=list(r.point_ids),
        ordered=r.ordered,
        allowed_minutes=r.allowed_minutes,
        slot=r.slot,
        started_by=r.started_by,
        started_at=r.started_at,
        finished_at=r.finished_at,
        scans=[
            PatrolScanOut(
                point_id=s.point_id,
                at=s.scanned_at,
                note=s.note,
                out_of_order=s.out_of_order,
                by=s.scanned_by,
            )
            for s in v.scans
        ],
        report=_report(v.report),
    )


class PatrolSlotOut(BaseModel):
    plan_id: uuid.UUID
    plan_name: str
    slot: str
    status: SlotStatus
    run_id: uuid.UUID | None
    report: PatrolReportOut | None


DayQuery = Annotated[dt.date | None, Query(alias="date", description="boşsa bugün (tesis saati)")]


@router.get("/patrol-schedule", summary="Günün devriye takvimi")
async def schedule(
    ctx: Runner, today: TodayDep, now: NowDep, day: DayQuery = None
) -> list[PatrolSlotOut]:
    return [
        PatrolSlotOut(
            plan_id=s.plan.id,
            plan_name=s.plan.name,
            slot=s.slot,
            status=s.status,
            run_id=s.run.run.id if s.run else None,
            report=_report(s.run.report) if s.run else None,
        )
        for s in await svc.schedule(ctx.session, day or today, now=now)
    ]


@router.get("/patrol-runs", summary="Yürütülen turlar (gün; yeni üstte)")
async def list_runs(
    ctx: Runner, today: TodayDep, now: NowDep, day: DayQuery = None
) -> list[PatrolRunOut]:
    return [_run(v) for v in await svc.runs_on(ctx.session, day or today, now=now)]


class PatrolRunIn(BaseModel):
    plan_id: uuid.UUID | None = None
    slot: str | None = Field(default=None, description="SS:DD; boşsa şu andan önceki son saat")


async def _view(ctx: SiteContext, run: PatrolRun, now: dt.datetime) -> PatrolRunOut:
    [view] = await svc.run_views(ctx.session, [run], now=now)
    return _run(view)


@router.post("/patrol-runs", status_code=status.HTTP_201_CREATED, summary="Turu başlat")
async def start_run(
    ctx: Runner, body: PatrolRunIn, current: CurrentUserDep, now: NowDep
) -> Written[PatrolRunOut]:
    plan = await svc.get_plan(ctx.session, body.plan_id) if body.plan_id else None
    if plan is None:
        raise rule_error(FormFieldError({"plan_id": "Tur seçin."}))
    try:
        run = await svc.start_run(
            ctx.session,
            plan,
            slot=body.slot,
            now=now,
            actor=current.user.full_name,
            user_id=current.user.id,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _view(ctx, run, now),
        message=f"{plan.name} başladı. Her noktada etiketi okutun ya da kodunu girin.",
    )


async def _locked_run(ctx: SiteContext, run_id: uuid.UUID) -> PatrolRun:
    run = await svc.get_run(ctx.session, run_id, lock=True)
    if run is None:
        raise NotFoundError("Tur bulunamadı.")
    return run


class PatrolScanIn(BaseModel):
    code: str | None = Field(default=None, max_length=40, description="etiketteki 6 haneli kod")
    note: str | None = Field(default=None, max_length=500)


@router.post("/patrol-runs/{run_id}/scans", summary="Nokta okut")
async def scan(
    run_id: uuid.UUID, ctx: Runner, body: PatrolScanIn, current: CurrentUserDep, now: NowDep
) -> Written[PatrolRunOut]:
    run = await _locked_run(ctx, run_id)
    try:
        row, point = await svc.scan(
            ctx.session, run, code=body.code, note=body.note, now=now, actor=current.user.full_name
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    out = await _view(ctx, run, now)
    extra = " — sıra dışı" if row.out_of_order else ""
    return Written(
        data=out,
        message=f"{point.name} okutuldu{extra}. "
        f"({out.report.scanned_points}/{out.report.total_points})",
    )


@router.post("/patrol-runs/{run_id}/finish", summary="Turu bitir (sapma raporu)")
async def finish(
    run_id: uuid.UUID, ctx: Runner, current: CurrentUserDep, now: NowDep
) -> Written[PatrolRunOut]:
    run = await _locked_run(ctx, run_id)
    try:
        await svc.finish(ctx.session, run, ctx.site, now=now, actor=current.user.full_name)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    out = await _view(ctx, run, now)
    rep = out.report
    if rep.ok:
        message = "Tur sorunsuz tamamlandı."
    else:
        parts = [
            f"{len(rep.missed)} nokta okutulmadı" if rep.missed else "",
            f"{len(rep.out_of_order)} sıra dışı" if rep.out_of_order else "",
            "süre aşıldı" if rep.overtime else "",
        ]
        message = f"Tur bitti — sapma: {', '.join(p for p in parts if p)}. Yöneticiye özet gitti."
    return Written(data=out, message=message)
