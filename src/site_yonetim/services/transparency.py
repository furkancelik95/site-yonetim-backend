"""Şeffaflık raporu — Yazılım Dokümanı RAP-02, PM kararı K32 ("YK onaylı özet").
Açık site kapsamında; commit çağırana ait.

- Sakin, sitenin tüm giderlerini ve fatura görüntüsünü **görmez**; yerine kişi ve bölüm bilgisi
  içermeyen aylık gelir–gider özeti yayınlanır.
- Muhasebe/yönetici dönemi seçip taslak hazırlar: tutarlar o an hesaplanır ve **donar**
  (`content`); kapsam (kategori dökümü, aidat tahsilat oranı, kasa+banka bakiyesi) YK'nın
  belirlediği şekilde seçilir. Tedarikçi adı, gider açıklaması, belge ve kişi yoktur.
- Yayın "Şeffaflık raporu" onay kuralından geçer (`transparency_report`). Kural tanımlı değilse
  `transparency.publish` izni olan (yönetici) doğrudan yayınlar. Ret → rapor "reddedildi",
  gerekçesiyle; aynı dönem için yeni taslak hazırlanabilir.
- Yayında sakinlere uygulama içi bildirim gider.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType, ApprovalStatus, Decision
from site_yonetim.domain.communication import TransparencyStatus, check_period
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import format_period_tr
from site_yonetim.models import Site, SiteMembership, TransparencyReport
from site_yonetim.models.approvals import ApprovalDecision, ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import cash
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.reports import month_summary

NOTE_MAX = 1000
SCOPE_KEYS = ("include_categories", "include_collection", "include_cash_balance")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


async def _content(
    session: AsyncSession, year: int, month: int, scope: dict[str, bool]
) -> dict[str, Any]:
    summary = await month_summary(session, year, month)
    content: dict[str, Any] = {
        "period": format_period_tr(year, month),
        "income": _money(summary.income),
        "expense": _money(summary.expense),
        "difference": _money(summary.income - summary.expense),
        "categories": None,
        "collection": None,
        "cash_balance": None,
    }
    if scope["include_categories"]:
        content["categories"] = [
            {
                "name": c.name,
                "amount": _money(c.amount),
                "count": c.count,
                "share": f"{c.share:.2f}",
            }
            for c in summary.categories
        ]
    if scope["include_collection"] and summary.collection is not None:
        c = summary.collection
        content["collection"] = {
            "charged": _money(c.charged),
            "collected": _money(c.collected),
            "rate": f"{c.rate:.2f}",
        }
    if scope["include_cash_balance"]:
        content["cash_balance"] = _money(await cash.total_balance(session))
    return content


async def prepare(
    session: AsyncSession,
    *,
    year: int,
    month: int,
    scope: dict[str, bool],
    note: str | None,
    by: str,
    today: date,
) -> TransparencyReport:
    problem = check_period(year, month, today)
    text = (note or "").strip() or None
    errors: dict[str, str] = {}
    if problem:
        errors["month"] = problem
    if text and len(text) > NOTE_MAX:
        errors["note"] = f"Not en fazla {NOTE_MAX} karakter."
    if errors:
        raise FormFieldError(errors)
    chosen = {key: bool(scope.get(key, True)) for key in SCOPE_KEYS}
    report = TransparencyReport(
        id=uuid.uuid7(),
        year=year,
        month=month,
        status=TransparencyStatus.DRAFT.value,
        scope=chosen,
        content=await _content(session, year, month, chosen),
        note=text,
        prepared_by=by,
    )
    session.add(report)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise OperationRuleError(
            "exists",
            f"{format_period_tr(year, month)} dönemi için rapor zaten var; reddedilmediyse yenisi "
            "hazırlanamaz.",
            conflict=True,
        ) from exc
    return report


async def get(
    session: AsyncSession, report_id: uuid.UUID, *, lock: bool = False
) -> TransparencyReport | None:
    query = select(TransparencyReport).where(TransparencyReport.id == report_id)
    found: TransparencyReport | None = await session.scalar(
        query.with_for_update() if lock else query
    )
    return found


async def delete_draft(session: AsyncSession, report: TransparencyReport) -> None:
    if report.status not in {TransparencyStatus.DRAFT.value, TransparencyStatus.REJECTED.value}:
        raise OperationRuleError(
            "invalid_status", "Yalnız taslak ya da reddedilen rapor silinir.", conflict=True
        )
    await session.delete(report)
    await session.flush()


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    report: TransparencyReport
    approval_request_id: uuid.UUID | None  # onaya gittiyse
    notified: int


async def submit(
    session: AsyncSession,
    site: Site,
    report: TransparencyReport,
    *,
    requester: approvals_svc.Person,
    can_publish: bool,
    today: date,
    now: datetime,
) -> PublishOutcome:
    """Onay kuralı varsa onaya gönderir; yoksa yetkili doğrudan yayınlar."""
    if report.status != TransparencyStatus.DRAFT.value:
        raise OperationRuleError(
            "invalid_status", "Yalnız taslak rapor yayına gönderilir.", conflict=True
        )
    version = await approvals_svc.match_rule(
        session, ActionType.TRANSPARENCY_REPORT, amount=None, source=None, today=today
    )
    if version is not None:
        period = format_period_tr(report.year, report.month)
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.TRANSPARENCY_REPORT,
                title=f"Şeffaflık raporu — {period}",
                summary={
                    "period": period,
                    "income": report.content.get("income"),
                    "expense": report.content.get("expense"),
                },
                payload={"report_id": str(report.id)},
                subject_type="transparency_reports",
                subject_id=report.id,
            ),
            requester=requester,
            today=today,
            now=now,
        )
        report.status = TransparencyStatus.PENDING.value
        report.approval_request_id = request.id
        await session.flush()
        return PublishOutcome(report, request.id, 0)
    if not can_publish:
        raise PermissionError("Onay kuralı tanımlı değil; raporu site yöneticisi yayınlar.")
    notified = await _publish(session, report, by=requester.name, now=now)
    return PublishOutcome(report, None, notified)


async def _resident_users(session: AsyncSession) -> list[uuid.UUID]:
    return list(
        await session.scalars(
            select(SiteMembership.user_id).where(
                SiteMembership.person_id.is_not(None), SiteMembership.is_active
            )
        )
    )


async def _publish(
    session: AsyncSession, report: TransparencyReport, *, by: str, now: datetime
) -> int:
    report.status = TransparencyStatus.PUBLISHED.value
    report.published_at = now
    report.published_by = by
    report.rejection_reason = None
    await session.flush()
    period = format_period_tr(report.year, report.month)
    return await notify_svc.notify(
        session,
        await _resident_users(session),
        notify_svc.Message(
            category=NotificationCategory.ANNOUNCEMENT,
            kind="transparency.published",
            title=f"{period} şeffaflık raporu yayınlandı",
            body="Sitenin aylık gelir–gider özeti uygulamada.",
            link_entity="transparency_reports",
            link_id=report.id,
        ),
        dedupe_key=f"transparency:{report.id}",
    )


async def _of(session: AsyncSession, request: ApprovalRequest) -> TransparencyReport | None:
    raw = (request.payload or {}).get("report_id")
    return await get(session, uuid.UUID(raw), lock=True) if raw else None


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    report = await _of(session, request)
    if report is None or report.status != TransparencyStatus.PENDING.value:
        raise approvals_svc.ExecutionError("Rapor artık yayınlanacak durumda değil.")
    await _publish(
        session, report, by=request.requested_by_name, now=request.decided_at or report.created_at
    )


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    """Ret ya da geri çekme: ret gerekçesiyle "reddedildi"; geri çekmede taslağa döner."""
    report = await _of(session, request)
    if report is None or report.status != TransparencyStatus.PENDING.value:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        comment = await session.scalar(
            select(ApprovalDecision.comment)
            .where(
                ApprovalDecision.request_id == request.id,
                ApprovalDecision.decision == Decision.REJECT.value,
            )
            .order_by(ApprovalDecision.created_at.desc())
            .limit(1)
        )
        report.status = TransparencyStatus.REJECTED.value
        report.rejection_reason = comment or "Onaycı reddetti."
    else:
        report.status = TransparencyStatus.DRAFT.value
        report.approval_request_id = None
    await session.flush()


approvals_svc.register_executor(ActionType.TRANSPARENCY_REPORT, _execute)
approvals_svc.register_closer(ActionType.TRANSPARENCY_REPORT, _close)
