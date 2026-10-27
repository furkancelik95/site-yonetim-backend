"""Dönem kapanışı ve kilit — Yazılım Dokümanı FIN-13, servis isteği 27.

Okuma `finance.read`; kapanış/kilit/açma `finance.charge.post`. Dönem anahtarı `YYYY-MM`.
Kilitli/kapanıştaki döneme her finansal yazma 409 `period_locked` alır (`services/period_lock`).
Süreli açma, "Dönem kilidi açma" onay kuralı varsa 202 döner (talep onaya gider).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.finance_common import ChargePost, FinanceRead, finance_error
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, period_param
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, PeriodStatus
from site_yonetim.domain.fiscal import CLOSE_ITEMS, period_key, period_label
from site_yonetim.services import periods as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/fiscal-periods", tags=["dönem kapanışı"])


class PeriodEventOut(BaseModel):
    at: dt.datetime
    by: str
    action: str = Field(
        description="start_close · reopen · lock · unlock_requested · unlock · unlock_rejected · "
        "relocked"
    )
    note: str | None


class FiscalPeriodOut(BaseModel):
    period: str = Field(description="YYYY-MM")
    label: str = Field(description='"Eylül 2026"')
    status: PeriodStatus
    is_past: bool = Field(description="içinde bulunulan aydan önce (kapanışa alınabilir)")
    exceptions: dict[str, str] = Field(description="kontrol listesi maddesi → gerekçe")
    closing_started_by: str | None
    closing_started_at: dt.datetime | None
    locked_by: str | None
    locked_at: dt.datetime | None
    unlock_until: dt.datetime | None
    unlock_reason: str | None
    unlock_request_id: uuid.UUID | None = Field(description="onay bekleyen süreli açma talebi")
    history: list[PeriodEventOut]


def _out(view: svc.PeriodView, today: dt.date) -> FiscalPeriodOut:
    row = view.row
    return FiscalPeriodOut(
        period=period_key(view.period),
        label=period_label(view.period),
        status=view.status,
        is_past=view.period < YearMonth.of(today),
        exceptions=dict(row.exceptions or {}) if row else {},
        closing_started_by=row.closing_started_by if row else None,
        closing_started_at=row.closing_started_at if row else None,
        locked_by=row.locked_by if row else None,
        locked_at=row.locked_at if row else None,
        unlock_until=row.unlock_until if row and view.status is PeriodStatus.TEMP_OPEN else None,
        unlock_reason=row.unlock_reason if row else None,
        unlock_request_id=row.unlock_request_id if row else None,
        history=[
            PeriodEventOut(at=e.created_at, by=e.by_name, action=e.action, note=e.note)
            for e in view.events
        ],
    )


@router.get("", summary="Yılın dönemleri (yeniden eskiye)")
async def list_periods(
    ctx: FinanceRead,
    today: TodayDep,
    now: NowDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> list[FiscalPeriodOut]:
    """İçinde bulunulan yıl için bu aya kadar; en fazla 12 satır."""
    views = await svc.year_overview(ctx.session, year or today.year, today, now)
    return [_out(v, today) for v in views]


class CloseCheckItemOut(BaseModel):
    key: str
    label: str
    ok: bool
    detail: str
    link: str = Field(description="ekrandaki ilgili sayfa (frontend yolu)")
    required: bool
    exception: str | None = Field(description="kapanışta girilen gerekçeli istisna")


class CloseChecklistOut(BaseModel):
    period: FiscalPeriodOut
    items: list[CloseCheckItemOut]


@router.get("/{period}/close-checklist", summary="Kapanış kontrol listesi")
async def close_checklist(
    period: str, ctx: FinanceRead, today: TodayDep, now: NowDep
) -> CloseChecklistOut:
    ym = period_param(period)
    view = await svc.view(ctx.session, ym, now)
    results = await svc.checklist(ctx.session, ym)
    exceptions = dict(view.row.exceptions or {}) if view.row else {}
    return CloseChecklistOut(
        period=_out(view, today),
        items=[
            CloseCheckItemOut(
                key=r.item.value,
                label=r.label,
                ok=r.ok,
                detail=r.detail,
                link=CLOSE_ITEMS[r.item][1],
                required=r.required,
                exception=exceptions.get(r.item.value),
            )
            for r in results
        ],
    )


class PeriodStartCloseIn(BaseModel):
    exceptions: dict[str, str] | None = Field(
        default=None, description="eksik zorunlu madde → gerekçe (en az 10 karakter)"
    )


async def _done(
    ctx: ChargePost, ym: YearMonth, today: dt.date, now: dt.datetime
) -> FiscalPeriodOut:
    await ctx.session.commit()
    return _out(await svc.view(ctx.session, ym, now), today)


@router.post("/{period}/start-close", summary="Kapanışı başlat (Açık → Kapanışta)")
async def start_close(
    period: str,
    body: PeriodStartCloseIn,
    ctx: ChargePost,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[FiscalPeriodOut]:
    """409: `period_not_ended`, `checklist_incomplete`, `invalid_status`."""
    ym = period_param(period)
    try:
        await svc.start_close(
            ctx.session,
            ym,
            exceptions=body.exceptions,
            actor=actor(current),
            today=today,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    return Written(
        data=await _done(ctx, ym, today, now),
        message=f"{period_label(ym)} kapanışta; yeni işlemler sonraki döneme yazılır. Kilitlemek "
        "için kapanışı başlatandan farklı bir yetkili onaylamalı.",
    )


@router.post("/{period}/lock", summary="Kilitle (iki kişi kuralı)")
async def lock(
    period: str, ctx: ChargePost, current: CurrentUserDep, today: TodayDep, now: NowDep
) -> Written[FiscalPeriodOut]:
    """409 `four_eyes`: kapanışı başlatan kişi kilitleyemez."""
    ym = period_param(period)
    try:
        await svc.lock(ctx.session, ym, actor=actor(current), now=now)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    return Written(
        data=await _done(ctx, ym, today, now),
        message=f"{period_label(ym)} kilitlendi; bu döneme finansal kayıt eklenemez ve "
        "değiştirilemez.",
    )


@router.post("/{period}/reopen", summary="Kapanıştan açığa al")
async def reopen(
    period: str, ctx: ChargePost, current: CurrentUserDep, today: TodayDep, now: NowDep
) -> Written[FiscalPeriodOut]:
    ym = period_param(period)
    try:
        await svc.reopen(ctx.session, ym, actor=actor(current), now=now)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    return Written(
        data=await _done(ctx, ym, today, now), message=f"{period_label(ym)} yeniden açık."
    )


class PeriodUnlockIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500, description="en az 20 karakter")
    hours: int | None = Field(default=None, description="4 · 24 · 72")


@router.post(
    "/{period}/unlock-requests",
    summary="Kilidi süreli aç (onay kuralı varsa 202)",
    responses={202: {"description": "Talep onaya gönderildi"}},
)
async def request_unlock(
    period: str,
    body: PeriodUnlockIn,
    ctx: ChargePost,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[FiscalPeriodOut]:
    ym = period_param(period)
    try:
        result = await svc.request_unlock(
            ctx.session,
            ctx.site,
            ym,
            reason=body.reason,
            hours=body.hours,
            actor=actor(current),
            today=today,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    data = await _done(ctx, ym, today, now)
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=data,
            message=f"Kilit açma talebi #{result.request.number} onaya gönderildi; onaylanınca "
            f"{period_label(ym)} {body.hours} saatliğine açılır.",
        )
    return Written(
        data=data,
        message=f"{period_label(ym)} {body.hours} saatliğine açıldı; bu sürede yapılan işlemler "
        'denetim kaydına "kilit açıkken" (period_unlocked) etiketiyle yazılır.',
    )
