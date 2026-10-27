"""Dönem kapanışı (FIN-13, servis isteği 27): kontrol listesi, kapanışı başlat, kilitle, açığa
al, kilidi süreli aç. Açık site kapsamında; kilit kontrolünün kendisi `services/period_lock.py`.

- Kapanış: zorunlu maddeler sağlanmalı ya da her biri için ≥ 10 karakter gerekçeli istisna
  (409 `checklist_incomplete`). İçinde bulunulan/gelecek ay kapanışa alınamaz.
- **İki kişi kuralı:** kilitleyen, kapanışı başlatandan farklı bir yetkili olmalı (409 `four_eyes`).
- Süreli açma: gerekçe ≥ 20 karakter, 4/24/72 saat. "Dönem kilidi açma" onay kuralı varsa (hazır
  şablon: YK çoğunluğu) talep onaya gider (202); onaylanınca dönem süreli açılır. Süre dolunca
  okurken kilitli sayılır; gece işi (`relock_expired`) durumu ve geçmişi günceller.
- Her durum değişikliği `period_events`'e (değişmez) ve denetim kaydına yazılır.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import ChargeRunStatus, FinanceRuleError, PeriodStatus
from site_yonetim.domain.fiscal import (
    CheckResult,
    CloseItem,
    check_can_start_close,
    check_unlock_request,
    clean_exceptions,
    effective_status,
    missing_items,
    period_label,
)
from site_yonetim.domain.text import format_date_tr
from site_yonetim.models import ChargeRun, Expense, Period, PeriodEvent, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import accounting_export, cash_days, period_lock
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services.approvals import ExecutionError, Person

# Kapanışı engelleyen bekleyen onaylar: finansal kaydı etkileyen işlem tipleri.
FINANCE_ACTIONS = frozenset(
    {
        ActionType.CHARGE_RULE,
        ActionType.CHARGE_RUN,
        ActionType.CHARGE_RUN_REVERSAL,
        ActionType.REFUND,
        ActionType.DEBT_WRITE_OFF,
        ActionType.EXPENSE,
        ActionType.EMERGENCY_EXPENSE,
        ActionType.PAYMENT_PLAN,
        ActionType.FINANCE_MODE_CHANGE,
        ActionType.CASH_DIFFERENCE,
        ActionType.AD_HOC_CHARGE,
        ActionType.FINANCE_PARAMETER,
    }
)


@dataclass(frozen=True, slots=True)
class PeriodView:
    period: YearMonth
    row: Period | None
    status: PeriodStatus
    events: list[PeriodEvent]


def _status(row: Period | None, now: datetime) -> PeriodStatus:
    return effective_status(row.status, row.unlock_until, now) if row else PeriodStatus.OPEN


async def _events(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PeriodEvent]]:
    out: dict[uuid.UUID, list[PeriodEvent]] = defaultdict(list)
    if ids:
        for event in await session.scalars(
            select(PeriodEvent)
            .where(PeriodEvent.period_id.in_(ids))
            .order_by(PeriodEvent.created_at)
        ):
            out[event.period_id].append(event)
    return out


async def view(session: AsyncSession, period: YearMonth, now: datetime) -> PeriodView:
    row = await period_lock.get_period(session, period)
    events = (await _events(session, [row.id]))[row.id] if row else []
    return PeriodView(period, row, _status(row, now), events)


async def year_overview(
    session: AsyncSession, year: int, today: date, now: datetime
) -> list[PeriodView]:
    """Yılın dönemleri, yeniden eskiye; içinde bulunulan yıl için bu aya kadar."""
    if year > today.year:
        return []
    last = today.month if year == today.year else 12
    rows = {
        r.month: r
        for r in await session.scalars(
            select(Period).where(Period.year == year, Period.month <= last)
        )
    }
    events = await _events(session, [r.id for r in rows.values()])
    return [
        PeriodView(
            YearMonth(year, m),
            rows.get(m),
            _status(rows.get(m), now),
            events.get(rows[m].id, []) if m in rows else [],
        )
        for m in range(last, 0, -1)
    ]


# --- Kontrol listesi ----------------------------------------------------------------------


async def checklist(session: AsyncSession, period: YearMonth) -> list[CheckResult]:
    first, last = period.first_day, period.last_day
    posted = await session.scalar(
        select(func.count())
        .select_from(ChargeRun)
        .join(Period, and_(Period.id == ChargeRun.period_id, Period.site_id == ChargeRun.site_id))
        .where(
            Period.year == period.year,
            Period.month == period.month,
            ChargeRun.status == ChargeRunStatus.POSTED.value,
            ChargeRun.reversal_of_run_id.is_(None),
        )
    )
    open_days = await cash_days.open_days_in(session, first, last)
    pending = await session.scalar(
        select(func.count()).where(
            ApprovalRequest.status.in_([s.value for s in OPEN_STATUSES]),
            ApprovalRequest.action_type.in_([a.value for a in FINANCE_ACTIONS]),
        )
    )
    unpaid = list(
        await session.scalars(
            select(Expense.description)
            .where(
                Expense.date >= first,
                Expense.date <= last,
                Expense.paid_on.is_(None),
                Expense.is_reversed.is_(False),
                Expense.reversal_of_id.is_(None),
            )
            .order_by(Expense.date)
        )
    )
    vouchers = await accounting_export.pending_count(session, period)
    days_text = ", ".join(f"{name} {format_date_tr(day)}" for name, day in open_days[:4])
    return [
        CheckResult(
            CloseItem.CHARGE,
            bool(posted),
            "Tahakkuk kaydedildi" if posted else "Bu dönem için kesinleşmiş tahakkuk yok",
        ),
        CheckResult(
            CloseItem.CASH_DAYS,
            not open_days,
            f"{len(open_days)} gün sayılmadı: {days_text}{'…' if len(open_days) > 4 else ''}"
            if open_days
            else "Hareketli tüm kasa günleri sayımla kapandı",
        ),
        CheckResult(
            CloseItem.APPROVALS,
            not pending,
            f"{pending} finans talebi onay bekliyor" if pending else "Bekleyen finans talebi yok",
        ),
        CheckResult(
            CloseItem.EXPENSES,
            not unpaid,
            f"{len(unpaid)} ödenmemiş gider: {', '.join(unpaid[:3])}"
            if unpaid
            else "Ödenmemiş gider yok",
        ),
        CheckResult(
            CloseItem.BANK,
            True,
            "Eşleşmeyen hareket kuyruğu henüz yok; banka aktarımında eşleşmeyen satır aktarılmaz",
        ),
        CheckResult(
            CloseItem.ACCOUNTING,
            vouchers == 0,
            f"{vouchers} fiş aktarılmadı" if vouchers else "Aktarılmamış fiş yok",
        ),
    ]


# --- Durum geçişleri ----------------------------------------------------------------------


def _event(row: Period, action: str, actor: Person | None, note: str | None = None) -> PeriodEvent:
    return PeriodEvent(
        period_id=row.id,
        action=action,
        note=note,
        by_user_id=actor.user_id if actor else None,
        by_name=actor.name if actor else "Sistem",
    )


async def _locked_row(session: AsyncSession, period: YearMonth) -> Period:
    row = await period_lock.get_or_create(session, period)
    locked: Period | None = await session.scalar(
        select(Period).where(Period.id == row.id).with_for_update()
    )
    if locked is None:  # pragma: no cover - aynı transaction'da az önce okundu
        raise FinanceRuleError("not_found", "Dönem bulunamadı.")
    return locked


async def start_close(
    session: AsyncSession,
    period: YearMonth,
    *,
    exceptions: dict[str, str] | None,
    actor: Person,
    today: date,
    now: datetime,
) -> Period:
    row = await _locked_row(session, period)
    check_can_start_close(_status(row, now), period, today)
    cleaned = clean_exceptions(exceptions)
    missing = missing_items(await checklist(session, period), cleaned)
    if missing:
        raise FinanceRuleError(
            "checklist_incomplete",
            f"Eksik maddeler var: {'; '.join(m.label for m in missing)}. Giderin ya da gerekçeli "
            "istisna girin (en az 10 karakter).",
            conflict=True,
        )
    row.status = PeriodStatus.CLOSING.value
    row.exceptions = cleaned
    row.closing_started_by_id = actor.user_id
    row.closing_started_by = actor.name
    row.closing_started_at = now
    note = f"{len(cleaned)} gerekçeli istisna" if cleaned else None
    session.add(_event(row, "start_close", actor, note))
    await session.flush()
    return row


async def lock(session: AsyncSession, period: YearMonth, *, actor: Person, now: datetime) -> Period:
    row = await _locked_row(session, period)
    status = _status(row, now)
    if status not in (PeriodStatus.CLOSING, PeriodStatus.TEMP_OPEN):
        raise FinanceRuleError(
            "invalid_status",
            "Yalnız kapanıştaki ya da süreli açılmış dönem kilitlenir.",
            conflict=True,
        )
    if status is PeriodStatus.CLOSING and row.closing_started_by_id == actor.user_id:
        raise FinanceRuleError(
            "four_eyes",
            "Kilitleme, kapanışı başlatan kişiden farklı bir yetkili tarafından yapılmalı "
            "(iki kişi kuralı).",
            conflict=True,
        )
    row.status = PeriodStatus.LOCKED.value
    row.locked_by = actor.name
    row.locked_at = now
    row.unlock_until = None
    session.add(_event(row, "lock", actor))
    await session.flush()
    return row


async def reopen(
    session: AsyncSession, period: YearMonth, *, actor: Person, now: datetime
) -> Period:
    row = await _locked_row(session, period)
    if _status(row, now) is not PeriodStatus.CLOSING:
        raise FinanceRuleError(
            "invalid_status", "Yalnız kapanıştaki dönem açığa döner.", conflict=True
        )
    row.status = PeriodStatus.OPEN.value
    row.exceptions = {}
    row.closing_started_by_id = None
    row.closing_started_by = None
    row.closing_started_at = None
    session.add(_event(row, "reopen", actor))
    await session.flush()
    return row


@dataclass(frozen=True, slots=True)
class UnlockResult:
    row: Period
    request: ApprovalRequest | None


def _open_for(row: Period, hours: int, reason: str, moment: datetime) -> None:
    row.status = PeriodStatus.TEMP_OPEN.value
    row.unlock_until = moment + timedelta(hours=hours)
    row.unlock_reason = reason
    row.unlock_request_id = None


async def request_unlock(
    session: AsyncSession,
    site: Site,
    period: YearMonth,
    *,
    reason: str | None,
    hours: int | None,
    actor: Person,
    today: date,
    now: datetime,
) -> UnlockResult:
    text, picked = check_unlock_request(reason, hours)
    row = await _locked_row(session, period)
    if _status(row, now) is not PeriodStatus.LOCKED:
        raise FinanceRuleError(
            "invalid_status", "Yalnız kilitli dönem için açma istenir.", conflict=True
        )
    if row.unlock_request_id is not None:
        raise FinanceRuleError(
            "unlock_pending", "Bu dönem için onay bekleyen bir açma talebi var.", conflict=True
        )
    if row.status == PeriodStatus.TEMP_OPEN.value:  # süresi dolmuş: önce kilitli kaydet
        row.status, row.unlock_until = PeriodStatus.LOCKED.value, None
    rule = await approvals_svc.match_rule(
        session, ActionType.PERIOD_UNLOCK, amount=None, source=None, today=today
    )
    if rule is None:
        _open_for(row, picked, text, now)
        session.add(_event(row, "unlock", actor, f"{picked} saat — {text}"))
        await session.flush()
        return UnlockResult(row, None)
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.PERIOD_UNLOCK,
            title=f"Dönem kilidi açma — {period_label(period)}, {picked} saat",
            summary={
                "period": f"{period.year:04d}-{period.month:02d}",
                "hours": picked,
                "reason": text,
            },
            payload={"period_id": str(row.id), "hours": picked, "reason": text},
            subject_type="periods",
            subject_id=row.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    row.unlock_request_id = request.id
    session.add(_event(row, "unlock_requested", actor, f"{picked} saat — {text}"))
    await session.flush()
    return UnlockResult(row, request)


async def _execute_unlock(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    row = await session.get(Period, uuid.UUID(payload["period_id"]))
    if row is None or row.unlock_request_id != request.id:
        raise ExecutionError("Dönem artık bu açma talebini beklemiyor.")
    moment = request.decided_at or datetime.now().astimezone()
    _open_for(row, int(payload["hours"]), str(payload["reason"]), moment)
    session.add(
        PeriodEvent(
            period_id=row.id,
            action="unlock",
            note=f"{payload['hours']} saat — onaylandı",
            by_user_id=request.requested_by,
            by_name=request.requested_by_name,
        )
    )
    await session.flush()


async def _close_unlock(session: AsyncSession, request: ApprovalRequest) -> None:
    row = await session.get(Period, uuid.UUID((request.payload or {})["period_id"]))
    if row is not None and row.unlock_request_id == request.id:
        row.unlock_request_id = None
        session.add(
            PeriodEvent(
                period_id=row.id,
                action="unlock_rejected",
                note=None,
                by_user_id=request.requested_by,
                by_name=request.requested_by_name,
            )
        )
        await session.flush()


approvals_svc.register_executor(ActionType.PERIOD_UNLOCK, _execute_unlock)
approvals_svc.register_closer(ActionType.PERIOD_UNLOCK, _close_unlock)


async def relock_expired(session: AsyncSession, now: datetime) -> int:
    """Gece/saatlik iş: süresi dolan süreli açmaları kilitler, geçmişe yazar."""
    rows = list(
        await session.scalars(
            select(Period)
            .where(Period.status == PeriodStatus.TEMP_OPEN.value, Period.unlock_until <= now)
            .with_for_update()
        )
    )
    for row in rows:
        row.status = PeriodStatus.LOCKED.value
        row.unlock_until = None
        session.add(_event(row, "relocked", None, "Süreli açma süresi doldu"))
    await session.flush()
    return len(rows)
