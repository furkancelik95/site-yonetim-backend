"""Dönem kilidi — **merkezi kontrol** (D-11; Yazılım Dokümanı FIN-13, YAP-02). Açık site kapsamında.

Kural: kapanıştaki ya da kilitli döneme **hiçbir finansal kayıt eklenemez, değişemez**. Her
finansal yazma yolu, yazdığı (ve ters kaydında değiştirdiği) kaydın tarihi için
`ensure_writable` çağırır: tahakkuk ve ters kaydı, tahsilat, iade, devir bakiye, gider (kayıt,
ödeme, düzeltme), kasa hareketi (elle, aktarım, geri alma — `cash.add_movement` içinden),
dönem dışı ücret ve iptali, ödeme planı. Reddedilen yazma 409 `period_locked` alır; mesajda en
erken açık dönem yazar.

Süreli açılmış (onaylı) dönemde yazma serbesttir; her yazma denetim kaydına `period_unlocked`
gerekçesiyle ayrıca işlenir. Süresi dolan süreli açma okurken de kilitli sayılır.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import PeriodStatus
from site_yonetim.domain.fiscal import (
    WRITE_BLOCKING,
    earliest_open,
    effective_status,
    locked_error,
    period_key,
)
from site_yonetim.models import Period
from site_yonetim.models.audit import AuditAction, record

_TAGGED = "period_unlocked_tagged"


async def get_period(session: AsyncSession, period: YearMonth) -> Period | None:
    row: Period | None = await session.scalar(
        select(Period).where(Period.year == period.year, Period.month == period.month)
    )
    return row


async def get_or_create(session: AsyncSession, period: YearMonth) -> Period:
    """Dönem satırı; yoksa açık olarak oluşturulur (docs/04 §7.1)."""
    row = await get_period(session, period)
    if row is None:
        row = Period(year=period.year, month=period.month, status=PeriodStatus.OPEN.value)
        session.add(row)
        await session.flush()
    return row


async def blocked_periods(session: AsyncSession, after: YearMonth, now: datetime) -> set[YearMonth]:
    """`after`'dan sonraki kapanıştaki/kilitli dönemler (en erken açık dönem hesabı için)."""
    rows = await session.execute(
        select(Period.year, Period.month, Period.status, Period.unlock_until).where(
            tuple_(Period.year, Period.month) > tuple_(after.year, after.month),
            Period.status != PeriodStatus.OPEN.value,
        )
    )
    return {
        YearMonth(r.year, r.month)
        for r in rows
        if effective_status(r.status, r.unlock_until, now) in WRITE_BLOCKING
    }


async def ensure_writable(
    session: AsyncSession, day: date, *, original: bool = False, now: datetime | None = None
) -> None:
    """`day` tarihli finansal kayıt yazılabilir mi? Değilse 409 `period_locked`.

    `original=True`: ters kaydı alınan / değiştirilen mevcut kaydın tarihi (orijinal değişmez).
    """
    moment = now or datetime.now(UTC)
    period = YearMonth.of(day)
    row = await get_period(session, period)
    if row is None:
        return
    status = effective_status(row.status, row.unlock_until, moment)
    if status in WRITE_BLOCKING:
        blocked = await blocked_periods(session, period, moment)
        raise locked_error(day, status, earliest_open(period, blocked), original=original)
    if status is PeriodStatus.TEMP_OPEN:
        tagged: set[object] = session.info.setdefault(_TAGGED, set())
        if row.id not in tagged:
            tagged.add(row.id)
            record(
                session,
                AuditAction.UPDATE,
                "periods",
                row.id,
                {"period": period_key(period), "unlocked_write_date": day.isoformat()},
                reason="period_unlocked",
            )
