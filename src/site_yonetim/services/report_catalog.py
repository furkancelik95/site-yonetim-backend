"""Rapor kataloğu: içerik üreticileri ve talep/SLA raporu (Yazılım Dokümanı RAP-01, RAP-05;
servis isteği 79).

**Kayıtlı içerik üreticisi arayüzü** (`register_producer`): her katalog raporu bir dönem için
`ReportContent` (özet satırları + tablolar + metrik tanımları) üretir. Ekrandaki Excel dışa
aktarma ve planlı rapor gönderimi (P16c `scheduled-reports`, RAP-05) **aynı** üreticiyi kullanır:

    content = await produce("requests_sla", session, ReportPeriod(start, end), now=now)
    data = exports.report_content_xlsx(content)

Üretici açık site kapsamında çalışır, yetki çağıranın sorumluluğundadır (katalogdaki izin).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, case, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.operations import RequestStatus
from site_yonetim.domain.report_catalog import BY_KEY, Metric, SlaCounts, SlaLine, sla_line
from site_yonetim.domain.text import format_period_tr
from site_yonetim.models import Request, RequestCategory, RequestFeedback
from site_yonetim.services import cash as cash_svc
from site_yonetim.services import reports as reports_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.exports import CellValue

SLA_WINDOW_DAYS = 30
MAX_PERIOD_DAYS = 366


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    start: date
    end: date  # dahil

    @property
    def label(self) -> str:
        return f"{self.start:%d.%m.%Y} – {self.end:%d.%m.%Y}"


@dataclass(frozen=True, slots=True)
class ReportTable:
    title: str
    columns: tuple[str, ...]
    rows: list[tuple[CellValue, ...]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReportContent:
    key: str
    title: str
    period: ReportPeriod
    summary: list[tuple[str, CellValue]]
    tables: list[ReportTable]
    metrics: tuple[Metric, ...]


Producer = Callable[[AsyncSession, ReportPeriod, datetime], Awaitable[ReportContent]]
_PRODUCERS: dict[str, Producer] = {}


def register_producer(key: str) -> Callable[[Producer], Producer]:
    if key not in BY_KEY:
        raise ValueError(f"Katalogda olmayan rapor: {key}")

    def wrap(fn: Producer) -> Producer:
        _PRODUCERS[key] = fn
        return fn

    return wrap


def has_producer(key: str) -> bool:
    return key in _PRODUCERS


def producer_keys() -> list[str]:
    return sorted(_PRODUCERS)


async def produce(
    key: str, session: AsyncSession, period: ReportPeriod, *, now: datetime
) -> ReportContent:
    return await _PRODUCERS[key](session, period, now)


def _bounds(period: ReportPeriod) -> tuple[datetime, datetime]:
    return (
        datetime.combine(period.start, time.min, BUSINESS_TZ),
        datetime.combine(period.end + timedelta(days=1), time.min, BUSINESS_TZ),
    )


def sla_window(to: date, start: date | None = None) -> ReportPeriod:
    return ReportPeriod(start or to - timedelta(days=SLA_WINDOW_DAYS - 1), to)


# --- Talep ve SLA ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlaRow:
    category: str
    category_name: str
    line: SlaLine


@dataclass(frozen=True, slots=True)
class SlaReport:
    period: ReportPeriod
    total: SlaLine
    rows: list[SlaRow]


async def requests_sla(session: AsyncSession, period: ReportPeriod) -> SlaReport:
    """Toplamalar veritabanında. Açılan/açık: dönemde açılan talepler; çözülen, SLA uyumu,
    ortalama süre, memnuniyet: dönemde çözülen (çözüldü/kapandı) talepler."""
    start, end = _bounds(period)
    finished = (RequestStatus.RESOLVED.value, RequestStatus.CLOSED.value)
    inactive = (*finished, RequestStatus.CANCELLED.value)
    created = and_(Request.created_at >= start, Request.created_at < end)
    resolved = and_(
        Request.status.in_(finished),
        Request.resolved_at.is_not(None),
        Request.resolved_at >= start,
        Request.resolved_at < end,
    )
    with_sla = and_(resolved, Request.sla_target_hours.is_not(None), Request.due_at.is_not(None))
    timed = and_(resolved, Request.resolved_at >= Request.created_at)
    latest = (
        select(RequestFeedback.request_id, RequestFeedback.score)
        .ext(distinct_on(RequestFeedback.request_id))
        .order_by(
            RequestFeedback.request_id,
            RequestFeedback.created_at.desc(),
            RequestFeedback.id.desc(),
        )
        .subquery()
    )
    rated = and_(resolved, latest.c.score.is_not(None))

    def count(condition: ColumnElement[bool]) -> ColumnElement[int]:
        return func.count().filter(condition)

    rows = await session.execute(
        select(
            Request.category,
            count(created),
            count(and_(created, Request.status.not_in(inactive))),
            count(resolved),
            count(with_sla),
            count(and_(with_sla, Request.resolved_at <= Request.due_at)),
            func.coalesce(
                func.sum(
                    case(
                        (timed, func.extract("epoch", Request.resolved_at - Request.created_at)),
                        else_=0,
                    )
                ),
                0,
            ),
            count(timed),
            func.coalesce(func.sum(case((rated, latest.c.score), else_=0)), 0),
            count(rated),
        )
        .outerjoin(latest, latest.c.request_id == Request.id)
        .where(created | resolved)
        .group_by(Request.category)
    )
    names = dict((await session.execute(select(RequestCategory.key, RequestCategory.name))).all())
    counts = [
        (
            category,
            SlaCounts(
                int(total),
                int(open_),
                int(closed),
                int(sla),
                int(met),
                Decimal(seconds),
                int(timed_n),
                int(score_sum),
                int(rated_n),
            ),
        )
        for category, total, open_, closed, sla, met, seconds, timed_n, score_sum, rated_n in rows
    ]
    total = sum((c for _, c in counts), SlaCounts())
    out = [SlaRow(key, names.get(key, key), sla_line(c)) for key, c in counts]
    out.sort(key=lambda r: (-r.line.total, -r.line.closed, r.category_name))
    return SlaReport(period, sla_line(total), out)


def _dec(value: Decimal | None) -> CellValue:
    return format(value, "f") if value is not None else None


@register_producer("requests_sla")
async def _sla_content(session: AsyncSession, period: ReportPeriod, _: datetime) -> ReportContent:
    report = await requests_sla(session, period)
    t = report.total
    header = ("Kategori", "Açılan", "Açık", "Çözülen", "SLA uyumu %", "Ort. çözüm (saat)",
              "Memnuniyet", "Puan sayısı")  # fmt: skip
    return ReportContent(
        "requests_sla",
        "Talep ve SLA raporu",
        period,
        [
            ("Açılan talep", t.total),
            ("Açık", t.open),
            ("Çözülen", t.closed),
            ("SLA uyumu %", _dec(t.sla_rate)),
            ("Ortalama çözüm (saat)", _dec(t.avg_hours)),
            ("Memnuniyet (1–5)", _dec(t.satisfaction)),
        ],
        [
            ReportTable(
                "Kategoriye göre",
                header,
                [
                    (
                        r.category_name,
                        r.line.total,
                        r.line.open,
                        r.line.closed,
                        _dec(r.line.sla_rate),
                        _dec(r.line.avg_hours),
                        _dec(r.line.satisfaction),
                        r.line.rated,
                    )
                    for r in report.rows
                ],
            )
        ],
        BY_KEY["requests_sla"].metrics,
    )


# --- Finans --------------------------------------------------------------------------------


def _months(period: ReportPeriod) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, month = period.start.year, period.start.month
    while (year, month) <= (period.end.year, period.end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


@dataclass(frozen=True, slots=True)
class PeriodFinance:
    """Dönemin finansal özeti (aylık çözünürlük: başlangıç ayından bitiş ayına)."""

    months: list[tuple[str, Decimal, Decimal]]  # dönem, gelir, gider
    categories: list[tuple[str, Decimal, int]]  # ad, tutar, adet
    budget: list[tuple[str, Decimal, Decimal, Decimal]]  # ad, bütçe, gerçekleşen, kullanım %
    budget_year: int
    collections: list[tuple[str, Decimal, Decimal, Decimal]]  # dönem, kesilen, tahsil, oran %

    @property
    def income(self) -> Decimal:
        return sum((m[1] for m in self.months), Decimal("0.00"))

    @property
    def expense(self) -> Decimal:
        return sum((m[2] for m in self.months), Decimal("0.00"))


async def period_finance(session: AsyncSession, period: ReportPeriod, today: date) -> PeriodFinance:
    wanted = set(_months(period))
    months: list[tuple[str, Decimal, Decimal]] = []
    categories: dict[str, tuple[Decimal, int]] = {}
    collections: list[tuple[str, Decimal, Decimal, Decimal]] = []
    yearly = None
    for year in sorted({y for y, _ in wanted}):
        yearly = await reports_svc.income_expense(session, year, today)
        months += [
            (format_period_tr(year, m.month), m.income, m.expense)
            for m in yearly.months
            if (year, m.month) in wanted
        ]
        for c in yearly.categories:
            amount, count = categories.get(c.name, (Decimal("0.00"), 0))
            categories[c.name] = (amount + c.amount, count + c.count)
        collections += [
            (format_period_tr(r.year, r.month), r.charged, r.collected, r.rate)
            for r in await reports_svc.collections(session, year)
            if (r.year, r.month) in wanted
        ]
    budget = [(b.name, b.budgeted, b.actual, b.usage) for b in yearly.budget] if yearly else []
    return PeriodFinance(
        months,
        sorted(((n, a, c) for n, (a, c) in categories.items()), key=lambda x: -x[1]),
        budget,
        period.end.year,
        collections,
    )


@register_producer("income_expense")
async def _income_expense_content(
    session: AsyncSession, period: ReportPeriod, now: datetime
) -> ReportContent:
    data = await period_finance(session, period, now.astimezone(BUSINESS_TZ).date())
    return ReportContent(
        "income_expense",
        "Gelir–gider raporu",
        period,
        [("Gelir", data.income), ("Gider", data.expense), ("Fark", data.income - data.expense)],
        [
            ReportTable("Aylar", ("Dönem", "Gelir", "Gider"), list(data.months)),
            ReportTable(
                "Gider kategorileri (yıllık)", ("Kategori", "Tutar", "Adet"), list(data.categories)
            ),
            ReportTable(
                f"Bütçe–gerçekleşme {data.budget_year}",
                ("Kategori", "Bütçe", "Gerçekleşen", "Kullanım %"),
                [(n, b, a, format(u, ".2f")) for n, b, a, u in data.budget],
            ),
        ],
        BY_KEY["income_expense"].metrics,
    )


@register_producer("collections")
async def _collections_content(
    session: AsyncSession, period: ReportPeriod, now: datetime
) -> ReportContent:
    data = await period_finance(session, period, now.astimezone(BUSINESS_TZ).date())
    charged = sum((c[1] for c in data.collections), Decimal("0.00"))
    collected = sum((c[2] for c in data.collections), Decimal("0.00"))
    rate = (collected / charged * 100).quantize(Decimal("0.01")) if charged else Decimal(0)
    return ReportContent(
        "collections",
        "Aidat tahsilat özeti",
        period,
        [("Kesilen", charged), ("Tahsil edilen", collected), ("Tahsilat oranı %", f"{rate:.2f}")],
        [
            ReportTable(
                "Dönemler",
                ("Dönem", "Kesilen", "Tahsil edilen", "Oran %"),
                [(p, c, t, f"{r:.2f}") for p, c, t, r in data.collections],
            )
        ],
        BY_KEY["collections"].metrics,
    )


@register_producer("cash")
async def _cash_content(session: AsyncSession, period: ReportPeriod, _: datetime) -> ReportContent:
    accounts = await cash_svc.accounts_with_balances(session)
    active = [a for a in accounts if a.account.is_active]
    return ReportContent(
        "cash",
        "Kasa ve banka özeti",
        period,
        [("Toplam bakiye (aktif hesaplar)", sum((a.balance for a in active), Decimal("0.00")))],
        [
            ReportTable(
                "Hesaplar",
                ("Hesap", "Giriş", "Çıkış", "Bakiye", "Aktif"),
                [
                    (a.account.name, a.inflow, a.outflow, a.balance, a.account.is_active)
                    for a in accounts
                ],
            )
        ],
        BY_KEY["cash"].metrics,
    )
