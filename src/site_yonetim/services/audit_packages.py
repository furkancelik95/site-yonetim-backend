"""Denetim kurulu paketi (Yazılım Dokümanı RAP-05; servis isteği 81).

Hazırlama (`finance.charge.post`): ad, dönem, arşivden belgeler. Paket o anki dönem özetini
(gelir–gider, kategori, bütçe–gerçekleşme, tahsilat, kasa/banka) ve dönemde karara bağlanmış
gündem maddelerini (karar defteri) **dondurur**. Denetçi (`audit.read`) raporunu Belgeler'den
seçip ekler → "denetlendi"; ikinci rapor eklenmez.
"""

import uuid
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.documents import Confidentiality
from site_yonetim.domain.management import MeetingStatus
from site_yonetim.models import AuditPackage, Document, Meeting, MeetingAgendaItem
from site_yonetim.services import cash as cash_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.report_catalog import ReportPeriod, period_finance

TITLE_MIN = 3
TITLE_MAX = 120
NOTE_MAX = 300
MAX_DOCUMENTS = 100
MAX_PERIOD_DAYS = 3 * 366


class PackageError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


def check_form(title: str | None, start: date | None, end: date | None) -> dict[str, str]:
    errors: dict[str, str] = {}
    clean = " ".join((title or "").split())
    if not TITLE_MIN <= len(clean) <= TITLE_MAX:
        errors["title"] = "Paket adını yazın (3–120 karakter)."
    if start is None:
        errors["from"] = "Dönem başını seçin."
    if end is None:
        errors["to"] = "Dönem sonunu seçin."
    elif start is not None and end < start:
        errors["to"] = "Bitiş başlangıçtan önce olamaz."
    elif start is not None and (end - start).days > MAX_PERIOD_DAYS:
        errors["to"] = "Dönem en fazla 3 yıl olabilir."
    return errors


async def _documents(
    session: AsyncSession, ids: Sequence[uuid.UUID], *, confidential: bool
) -> list[Document]:
    wanted = list(dict.fromkeys(ids))
    if len(wanted) > MAX_DOCUMENTS:
        raise PackageError(
            422, "validation_error", "En fazla 100 belge eklenebilir.", "document_ids"
        )
    if not wanted:
        return []
    query = select(Document).where(Document.id.in_(wanted))
    if not confidential:
        query = query.where(Document.confidentiality != Confidentiality.SECRET.value)
    found = {d.id: d for d in await session.scalars(query)}
    if len(found) != len(wanted):
        raise PackageError(
            422, "validation_error", "Seçilen belgelerden biri arşivde yok.", "document_ids"
        )
    return [found[i] for i in wanted]


async def decisions(session: AsyncSession, start: date, end: date) -> list[dict[str, Any]]:
    """Dönemde yapılmış toplantıların karara bağlanmış gündem maddeleri (karar defteri)."""
    lower = datetime.combine(start, time.min, BUSINESS_TZ)
    upper = datetime.combine(end + timedelta(days=1), time.min, BUSINESS_TZ)
    held = func.coalesce(Meeting.held_at, Meeting.scheduled_at)
    rows = await session.execute(
        select(Meeting, MeetingAgendaItem)
        .join(
            MeetingAgendaItem,
            and_(
                MeetingAgendaItem.meeting_id == Meeting.id,
                MeetingAgendaItem.site_id == Meeting.site_id,
            ),
        )
        .where(
            Meeting.status == MeetingStatus.HELD.value,
            MeetingAgendaItem.decision.is_not(None),
            held >= lower,
            held < upper,
        )
        .order_by(held, Meeting.number, MeetingAgendaItem.order)
    )
    out = []
    for no, (meeting, item) in enumerate(rows, start=1):
        moment = meeting.held_at or meeting.scheduled_at
        out.append(
            {
                "no": no,
                "meeting_title": meeting.title,
                "item_title": item.title,
                "decision": item.decision,
                "result": item.result,
                "date": moment.astimezone(BUSINESS_TZ).date().isoformat(),
            }
        )
    return out


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


async def snapshot(session: AsyncSession, start: date, end: date, today: date) -> dict[str, Any]:
    data = await period_finance(session, ReportPeriod(start, end), today)
    accounts = await cash_svc.accounts_with_balances(session)
    return {
        "prepared_on": today.isoformat(),
        "income": _money(data.income),
        "expense": _money(data.expense),
        "difference": _money(data.income - data.expense),
        "months": [
            {"period": p, "income": _money(i), "expense": _money(e)} for p, i, e in data.months
        ],
        "categories": [{"name": n, "amount": _money(a), "count": c} for n, a, c in data.categories],
        "budget_year": data.budget_year,
        "budget": [
            {"name": n, "budgeted": _money(b), "actual": _money(a), "usage": f"{u:.2f}"}
            for n, b, a, u in data.budget
        ],
        "collections": [
            {"period": p, "charged": _money(c), "collected": _money(t), "rate": f"{r:.2f}"}
            for p, c, t, r in data.collections
        ],
        "cash": [
            {"name": a.account.name, "balance": _money(a.balance), "is_active": a.account.is_active}
            for a in accounts
        ],
        "cash_total": _money(sum((a.balance for a in accounts if a.account.is_active), Decimal(0))),
    }


async def create(
    session: AsyncSession,
    *,
    title: str,
    start: date,
    end: date,
    document_ids: Sequence[uuid.UUID],
    confidential: bool,
    actor: str,
    today: date,
) -> AuditPackage:
    docs = await _documents(session, document_ids, confidential=confidential)
    package = AuditPackage(
        title=" ".join(title.split()),
        period_from=start,
        period_to=end,
        document_ids=[d.id for d in docs],
        decisions=await decisions(session, start, end),
        snapshot=await snapshot(session, start, end, today),
        created_by_name=actor,
    )
    session.add(package)
    await session.flush()
    return package


def list_query() -> Select[AuditPackage]:
    return select(AuditPackage).order_by(AuditPackage.created_at.desc(), AuditPackage.id)


async def get(
    session: AsyncSession, package_id: uuid.UUID, *, lock: bool = False
) -> AuditPackage | None:
    query = select(AuditPackage).where(AuditPackage.id == package_id)
    found: AuditPackage | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def add_report(
    session: AsyncSession,
    package: AuditPackage,
    *,
    document_id: uuid.UUID | None,
    note: str | None,
    actor: str,
    now: datetime,
) -> AuditPackage:
    if package.status == "reviewed":
        raise PackageError(409, "invalid_status", "Denetim raporu zaten eklendi; paket arşivlendi.")
    document = await session.get(Document, document_id) if document_id else None
    if document is None:
        raise PackageError(
            422, "validation_error", "Denetim raporu belgesini seçin.", "document_id"
        )
    clean = " ".join((note or "").split()) or None
    if clean and len(clean) > NOTE_MAX:
        raise PackageError(422, "validation_error", "Not en fazla 300 karakter.", "note")
    package.status = "reviewed"
    package.report_document_id = document.id
    package.reviewed_by_name = actor
    package.reviewed_at = now
    package.note = clean
    await session.flush()
    return package


async def document_titles(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Document.id, Document.title).where(Document.id.in_(ids)))
    return dict(rows.all())
