"""Devriye: kontrol noktaları, tur planı, yürütme, takvim ve sapma raporu (servis isteği 46;
Yazılım Dokümanı GUV-09). Site kapsamında; transaction'ı çağıran yönetir.

- Nokta kodu sunucuda üretilir (6 hane, site içinde tekil); etikete basılır.
- Yürütme planı dondurur; bir planın aynı anda tek açık yürütmesi olur (409 `run_open`).
- Okutma: plana ait olmayan nokta 409 `not_in_plan`, tekrar 409 `already_scanned`, sıralı turda
  beklenmeyen nokta `out_of_order` işaretlenip kabul edilir.
- Tur bitince sapma özeti yöneticilere (uygulama içi; dış kanal K5). Başlamayan / süresi aşan tur
  takibi 5 dakikalık gece işinde (`run-patrol-checks`).
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.patrol import (
    POINT_CODE_DIGITS,
    PatrolReport,
    PlanForm,
    PointForm,
    Scan,
    SlotStatus,
    build_report,
    current_slot,
    is_out_of_order,
    slot_status,
)
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import PatrolPlan, PatrolPoint, PatrolRun, PatrolScan, Site
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.gatehouse import manager_user_ids

CODE_ATTEMPTS = 50
RUNS_LIMIT = 200

# --- Noktalar ------------------------------------------------------------------------------


async def points(session: AsyncSession) -> list[PatrolPoint]:
    return list(await session.scalars(select(PatrolPoint).order_by(PatrolPoint.name_key)))


async def taken_names(session: AsyncSession, exclude: uuid.UUID | None) -> set[str]:
    query = select(PatrolPoint.name_key)
    if exclude is not None:
        query = query.where(PatrolPoint.id != exclude)
    return set(await session.scalars(query))


async def _new_code(session: AsyncSession) -> str:
    for _ in range(CODE_ATTEMPTS):
        code = f"{secrets.randbelow(10**POINT_CODE_DIGITS):0{POINT_CODE_DIGITS}d}"
        if await session.scalar(select(PatrolPoint.id).where(PatrolPoint.code == code)) is None:
            return code
    raise OperationRuleError(  # pragma: no cover - pratikte olmaz
        "code_unavailable", "Nokta kodu üretilemedi, tekrar deneyin.", conflict=True
    )


async def save_point(
    session: AsyncSession, point: PatrolPoint | None, form: PointForm, *, is_active: bool
) -> PatrolPoint:
    if point is None:
        point = PatrolPoint(code=await _new_code(session), is_active=True)
        session.add(point)
    else:
        point.is_active = is_active
    point.name, point.name_key = form.name, tr_lower(form.name)
    point.location, point.area, point.tag = form.location, form.area, form.tag.value
    await session.flush()
    return point


async def get_point(session: AsyncSession, point_id: uuid.UUID) -> PatrolPoint | None:
    row: PatrolPoint | None = await session.scalar(
        select(PatrolPoint).where(PatrolPoint.id == point_id)
    )
    return row


async def active_point_ids(session: AsyncSession) -> set[str]:
    return {
        str(i) for i in await session.scalars(select(PatrolPoint.id).where(PatrolPoint.is_active))
    }


# --- Planlar --------------------------------------------------------------------------------


async def plans(session: AsyncSession) -> list[PatrolPlan]:
    return list(await session.scalars(select(PatrolPlan).order_by(PatrolPlan.name, PatrolPlan.id)))


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> PatrolPlan | None:
    row: PatrolPlan | None = await session.scalar(
        select(PatrolPlan).where(PatrolPlan.id == plan_id)
    )
    return row


async def save_plan(
    session: AsyncSession, plan: PatrolPlan | None, form: PlanForm, *, is_active: bool
) -> PatrolPlan:
    if plan is None:
        plan = PatrolPlan(is_active=True)
        session.add(plan)
    else:
        plan.is_active = is_active
    plan.name = form.name
    plan.point_ids = [uuid.UUID(p) for p in form.point_ids]
    plan.ordered = form.ordered
    plan.start_times = list(form.start_times)
    plan.allowed_minutes = form.allowed_minutes
    plan.assignee = form.assignee
    await session.flush()
    return plan


# --- Yürütme ----------------------------------------------------------------------------------


def _local(now: datetime) -> datetime:
    return now.astimezone(BUSINESS_TZ)


def slot_start(day: date, slot: str) -> datetime:
    hour, minute = (int(x) for x in slot.split(":"))
    return datetime.combine(day, time(hour, minute), BUSINESS_TZ)


async def start_run(
    session: AsyncSession,
    plan: PatrolPlan,
    *,
    slot: str | None,
    now: datetime,
    actor: str,
    user_id: uuid.UUID,
) -> PatrolRun:
    if not plan.is_active:
        raise OperationRuleError("plan_inactive", "Tur seçin.", field="plan_id")
    times = tuple(plan.start_times)
    if slot is not None and slot not in times:
        raise OperationRuleError("slot_not_in_plan", "Bu saat turun planında yok.", field="slot")
    at = _local(now)
    open_run = await session.scalar(
        select(PatrolRun.id).where(PatrolRun.plan_id == plan.id, PatrolRun.finished_at.is_(None))
    )
    if open_run is not None:
        raise OperationRuleError("run_open", "Bu turun bitmemiş bir yürütmesi var.", conflict=True)
    run = PatrolRun(
        plan_id=plan.id,
        plan_name=plan.name,
        point_ids=list(plan.point_ids),
        ordered=plan.ordered,
        allowed_minutes=plan.allowed_minutes,
        slot=slot or current_slot(times, at.strftime("%H:%M")),
        run_date=at.date(),
        started_at=now,
        started_by=actor,
        started_by_user_id=user_id,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:  # eşzamanlı ikinci başlatma (kısmi tekil indeks)
        raise OperationRuleError(
            "run_open", "Bu turun bitmemiş bir yürütmesi var.", conflict=True
        ) from exc
    return run


async def get_run(
    session: AsyncSession, run_id: uuid.UUID, *, lock: bool = False
) -> PatrolRun | None:
    query = select(PatrolRun).where(PatrolRun.id == run_id)
    row: PatrolRun | None = await session.scalar(query.with_for_update() if lock else query)
    return row


def _finished(run: PatrolRun, message: str) -> None:
    if run.finished_at is not None:
        raise OperationRuleError("run_finished", message, conflict=True)


async def scan(
    session: AsyncSession,
    run: PatrolRun,
    *,
    code: str | None,
    note: str | None,
    now: datetime,
    actor: str,
) -> tuple[PatrolScan, PatrolPoint]:
    _finished(run, "Tur bitmiş.")
    digits = "".join(c for c in (code or "") if c.isdigit())
    point: PatrolPoint | None = (
        await session.scalar(select(PatrolPoint).where(PatrolPoint.code == digits))
        if len(digits) == POINT_CODE_DIGITS
        else None
    )
    if point is None:
        raise OperationRuleError("point_not_found", "Bu kodla nokta yok.", field="code")
    if point.id not in run.point_ids:
        raise OperationRuleError("not_in_plan", f"{point.name} bu turda yok.", conflict=True)
    scanned = {
        str(p)
        for p in await session.scalars(
            select(PatrolScan.point_id).where(PatrolScan.run_id == run.id)
        )
    }
    if str(point.id) in scanned:
        raise OperationRuleError("already_scanned", f"{point.name} zaten okutuldu.", conflict=True)
    row = PatrolScan(
        run_id=run.id,
        point_id=point.id,
        scanned_at=now,
        scanned_by=actor,
        note=" ".join((note or "").split())[:200] or None,
        out_of_order=is_out_of_order(
            run.ordered, tuple(str(p) for p in run.point_ids), scanned, str(point.id)
        ),
    )
    session.add(row)
    await session.flush()
    return row, point


async def finish(
    session: AsyncSession, run: PatrolRun, site: Site, *, now: datetime, actor: str
) -> PatrolRun:
    _finished(run, "Tur zaten bitmiş.")
    run.finished_at = now
    run.finished_by = actor
    await session.flush()
    [view] = await run_views(session, [run], now=now)
    report = view.report
    if report.ok:
        title = f"Devriye sorunsuz tamamlandı: {run.plan_name}"
        body = f"{report.scanned_points}/{report.total_points} nokta, {report.duration_minutes} dk"
    else:
        title = f"Devriye sapmalı tamamlandı: {run.plan_name}"
        body = deviation_text(report)
    await notify_svc.notify(
        session,
        await manager_user_ids(session, site),
        notify_svc.Message(
            category=NotificationCategory.SECURITY,
            kind="patrol.finished",
            title=title,
            body=body,
            link_entity="patrol_runs",
            link_id=run.id,
        ),
        dedupe_key=f"patrol-run:{run.id}:finished",
    )
    return run


def deviation_text(report: PatrolReport) -> str:
    parts = []
    if report.missed:
        parts.append(f"okutulmayan: {', '.join(report.missed)}")
    if report.out_of_order:
        parts.append(f"sıra dışı: {', '.join(report.out_of_order)}")
    if report.overtime:
        parts.append("süre aşıldı")
    if report.late_start_minutes > 15:
        parts.append(f"{report.late_start_minutes} dk geç başladı")
    return "; ".join(parts) or "sapma yok"


@dataclass(frozen=True, slots=True)
class RunView:
    run: PatrolRun
    scans: list[PatrolScan]
    report: PatrolReport


async def run_views(
    session: AsyncSession, runs: list[PatrolRun], *, now: datetime
) -> list[RunView]:
    ids = [r.id for r in runs]
    scans: dict[uuid.UUID, list[PatrolScan]] = {i: [] for i in ids}
    point_ids: set[uuid.UUID] = {p for r in runs for p in r.point_ids}
    if ids:
        for s in await session.scalars(
            select(PatrolScan)
            .where(PatrolScan.run_id.in_(ids))
            .order_by(PatrolScan.scanned_at, PatrolScan.id)
        ):
            scans[s.run_id].append(s)
            point_ids.add(s.point_id)
    names = (
        {
            str(i): n
            for i, n in await session.execute(
                select(PatrolPoint.id, PatrolPoint.name).where(PatrolPoint.id.in_(point_ids))
            )
        }
        if point_ids
        else {}
    )
    out = []
    for r in runs:
        report = build_report(
            point_ids=tuple(str(p) for p in r.point_ids),
            names=names,
            scans=[Scan(str(s.point_id), s.out_of_order) for s in scans[r.id]],
            started_at=r.started_at,
            finished_at=r.finished_at,
            now=now,
            allowed_minutes=r.allowed_minutes,
            slot_start=slot_start(r.run_date, r.slot) if r.slot else None,
        )
        out.append(RunView(r, scans[r.id], report))
    return out


async def runs_on(session: AsyncSession, day: date, *, now: datetime) -> list[RunView]:
    runs = list(
        await session.scalars(
            select(PatrolRun)
            .where(PatrolRun.run_date == day)
            .order_by(PatrolRun.started_at.desc(), PatrolRun.id.desc())
            .limit(RUNS_LIMIT)
        )
    )
    return await run_views(session, runs, now=now)


@dataclass(frozen=True, slots=True)
class SlotView:
    plan: PatrolPlan
    slot: str
    status: SlotStatus
    run: RunView | None


async def schedule(session: AsyncSession, day: date, *, now: datetime) -> list[SlotView]:
    """Günün planlı turları: her etkin planın her başlangıç saati, o saatin yürütmesiyle."""
    active = [p for p in await plans(session) if p.is_active]
    views = await runs_on(session, day, now=now)
    by_slot: dict[tuple[uuid.UUID, str | None], RunView] = {}
    for v in sorted(views, key=lambda v: v.run.started_at):
        by_slot.setdefault((v.run.plan_id, v.run.slot), v)
    out = []
    for plan in active:
        for slot in plan.start_times:
            view = by_slot.get((plan.id, slot))
            status = slot_status(
                slot_start=slot_start(day, slot),
                now=now,
                started_at=view.run.started_at if view else None,
                finished=bool(view and view.run.finished_at),
                report_ok=bool(view and view.report.ok),
                allowed_minutes=view.run.allowed_minutes if view else plan.allowed_minutes,
            )
            out.append(SlotView(plan, slot, status, view))
    return sorted(out, key=lambda s: (s.slot, s.plan.name))


# --- Takip işi (5 dakikada bir) ---------------------------------------------------------------


async def check_site(session: AsyncSession, site: Site, *, now: datetime) -> tuple[int, int]:
    """Başlamayan tur → yöneticiye anında bildirim; süresi aşan açık tur → bildirim. Aynı tur
    için bildirim bir kez (tekil anahtar). Dönen: (başlamayan, geciken) yeni bildirim sayısı."""
    today = _local(now).date()
    managers = await manager_user_ids(session, site)
    not_started = overdue = 0
    for slot in await schedule(session, today, now=now):
        if slot.status is SlotStatus.NOT_STARTED:
            not_started += bool(
                await notify_svc.notify(
                    session,
                    managers,
                    notify_svc.Message(
                        category=NotificationCategory.SECURITY,
                        kind="patrol.not_started",
                        title=f"Devriye başlamadı: {slot.plan.name} ({slot.slot})",
                        body="Planlanan saatten 15 dakika geçti; görevliye ulaşın.",
                        link_entity="patrol_plans",
                        link_id=slot.plan.id,
                    ),
                    dedupe_key=f"patrol:not-started:{slot.plan.id}:{today}:{slot.slot}",
                )
            )
    open_runs = await session.scalars(select(PatrolRun).where(PatrolRun.finished_at.is_(None)))
    for run in open_runs:
        if (now - run.started_at).total_seconds() > run.allowed_minutes * 60:
            overdue += bool(
                await notify_svc.notify(
                    session,
                    managers,
                    notify_svc.Message(
                        category=NotificationCategory.SECURITY,
                        kind="patrol.overdue",
                        title=f"Devriye süresi aşıldı: {run.plan_name}",
                        body=f"{run.started_by} turu {run.allowed_minutes} dk içinde bitirmedi.",
                        link_entity="patrol_runs",
                        link_id=run.id,
                    ),
                    dedupe_key=f"patrol:overdue:{run.id}",
                )
            )
    return not_started, overdue
