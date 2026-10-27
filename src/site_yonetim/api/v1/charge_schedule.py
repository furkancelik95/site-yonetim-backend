"""Otomatik aylık tahakkuk ayarı — frontend servis isteği 04 (backend issue #27).

Okuma `finance.read`, değiştirme `finance.charge.post`. Kesimi gece işi yapar
(`cli run-charge-schedules`, `services/charge_schedule.py`). Ayar değişikliği denetim kaydında.

K25: vade artık takvimde değil, finans parametresi `due_day` (varsayılan ayın 10'u); yanıtta
salt okunur döner. FIN-05: "Tahakkuk" onay kuralı varsa taslak kesimden `draft_lead_days` gün
önce hazırlanıp onaya gider (`approval_required`).
"""

import datetime as dt
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.finance_common import ChargePost, FinanceRead, finance_error
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, ScheduleRunStatus
from site_yonetim.models import ChargeSchedule
from site_yonetim.services import charge_drafts, finance_params
from site_yonetim.services import charge_schedule as svc
from site_yonetim.services.approvals import Person

router = APIRouter(prefix="/sites/{slug}", tags=["tahakkuk"])


class ScheduleIn(BaseModel):
    enabled: bool
    charge_day: int = Field(description="ayın kaçında: 1–28")
    draft_lead_days: int = Field(
        default=5, description="onay kuralı varsa taslak kesimden kaç gün önce: 0–15"
    )
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
    draft_lead_days: int
    due_day: int = Field(description="K25 vade günü — finans parametresi (salt okunur)")
    approval_required: bool = Field(
        description="'Tahakkuk' onay kuralı var: taslak hazırlanıp onaya gider (FIN-05)"
    )
    notify_on_run: bool = Field(description="bildirim sağlayıcısı yok (K5): şimdilik yalnız kayıt")
    next_run_on: dt.date | None = Field(description="kapalıysa null; İstanbul yerel tarihi")
    last_run: LastRunOut | None


async def _out(ctx: SiteContext, schedule: ChargeSchedule | None, today: dt.date) -> ScheduleOut:
    last = await svc.last_run(ctx.session)
    enabled, charge_day, lead, notify = (
        (schedule.enabled, schedule.charge_day, schedule.draft_lead_days, schedule.notify_on_run)
        if schedule
        else (False, svc.DEFAULT_DAY, svc.DEFAULT_LEAD_DAYS, True)
    )
    params = await finance_params.current(ctx.session, today)
    return ScheduleOut(
        enabled=enabled,
        charge_day=charge_day,
        draft_lead_days=lead,
        due_day=int(params["due_day"]),
        approval_required=await charge_drafts.rule_exists(ctx.session, today),
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
    """Hiç kaydedilmediyse varsayılan: kapalı, gün 1, taslak 5 gün önce."""
    return await _out(ctx, await svc.get(ctx.session), today)


@router.put("/charge-schedule", summary="Otomatik aylık tahakkuk ayarını kaydet")
async def put_schedule(
    ctx: ChargePost, body: ScheduleIn, today: TodayDep, current: CurrentUserDep
) -> Written[ScheduleOut]:
    try:
        schedule, turned_on = await svc.save(
            ctx.session,
            enabled=body.enabled,
            charge_day=body.charge_day,
            draft_lead_days=body.draft_lead_days,
            notify_on_run=body.notify_on_run,
            today=today,
            actor=Person(current.user.id, current.user.full_name),
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
