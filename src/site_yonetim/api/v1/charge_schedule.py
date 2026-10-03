"""Otomatik aylık tahakkuk ayarı — frontend servis isteği 04 (backend issue #27).

Okuma `finance.read`, değiştirme `finance.charge.post`. Kesimi gece işi yapar
(`cli run-charge-schedules`, `services/charge_schedule.py`). Ayar değişikliği denetim kaydında.
"""

import datetime as dt
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContext, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.finance_common import ChargePost, FinanceRead, finance_error
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, ScheduleRunStatus
from site_yonetim.models import ChargeSchedule
from site_yonetim.services import charge_schedule as svc

router = APIRouter(prefix="/sites/{slug}", tags=["tahakkuk"])


class ScheduleIn(BaseModel):
    enabled: bool
    charge_day: int = Field(description="ayın kaçında: 1–28")
    due_days: int = Field(description="tahakkuktan kaç gün sonra son ödeme: 0–60")
    notify_on_run: bool = True


class LastRunOut(BaseModel):
    run_id: uuid.UUID | None = Field(description="kesilen tahakkuk koşusu; atlandıysa null")
    period: str = Field(description="`10/2026`")
    ran_at: dt.datetime
    status: ScheduleRunStatus
    message: str | None


class ScheduleOut(BaseModel):
    enabled: bool
    charge_day: int
    due_days: int
    notify_on_run: bool = Field(description="bildirim sağlayıcısı yok (K5): şimdilik yalnız kayıt")
    next_run_on: dt.date | None = Field(description="kapalıysa null; İstanbul yerel tarihi")
    last_run: LastRunOut | None


async def _out(ctx: SiteContext, schedule: ChargeSchedule | None, today: dt.date) -> ScheduleOut:
    last = await svc.last_run(ctx.session)
    enabled, charge_day, due_days, notify = (
        (schedule.enabled, schedule.charge_day, schedule.due_days, schedule.notify_on_run)
        if schedule
        else (False, svc.DEFAULT_DAY, svc.DEFAULT_DUE_DAYS, True)
    )
    return ScheduleOut(
        enabled=enabled,
        charge_day=charge_day,
        due_days=due_days,
        notify_on_run=notify,
        next_run_on=await svc.next_run(ctx.session, schedule, today) if schedule else None,
        last_run=LastRunOut(
            run_id=last.charge_run_id,
            period=YearMonth(last.year, last.month).name,
            ran_at=last.ran_at,
            status=ScheduleRunStatus(last.status),
            message=last.message,
        )
        if last
        else None,
    )


@router.get("/charge-schedule", summary="Otomatik aylık tahakkuk ayarı")
async def get_schedule(ctx: FinanceRead, today: TodayDep) -> ScheduleOut:
    """Hiç kaydedilmediyse varsayılan: kapalı, gün 1, vade 14."""
    return await _out(ctx, await svc.get(ctx.session), today)


@router.put("/charge-schedule", summary="Otomatik aylık tahakkuk ayarını kaydet")
async def put_schedule(ctx: ChargePost, body: ScheduleIn, today: TodayDep) -> Written[ScheduleOut]:
    try:
        schedule, turned_on = await svc.save(
            ctx.session,
            enabled=body.enabled,
            charge_day=body.charge_day,
            due_days=body.due_days,
            notify_on_run=body.notify_on_run,
            today=today,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    out = await _out(ctx, schedule, today)
    await ctx.session.commit()
    if not schedule.enabled:
        message = "Otomatik tahakkuk kapatıldı."
    else:
        verb = "açıldı" if turned_on else "güncellendi"
        message = f"Otomatik tahakkuk {verb}; her ayın {schedule.charge_day}. günü kesilecek."
    return Written(data=out, message=message)
