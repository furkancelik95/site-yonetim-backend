"""Dönem, kalem tutarı ve kesim takvimi — saf (docs/04 §3, §7.2)."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from site_yonetim.domain.finance import Frequency
from site_yonetim.domain.money import distribute
from site_yonetim.domain.text import format_date_tr, format_period_tr

DEFAULT_DUE_DAY = 10  # K25: vade dönemin belirli günü — varsayılan ayın 10'u (FIN-16, §8.7)
DRAFT_LEAD_DAYS = 5  # FIN-05: taslak tahakkuk dönem başı − 5 gün
QUARTER_MONTHS = frozenset({1, 4, 7, 10})


@dataclass(frozen=True, slots=True, order=True)
class YearMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("Ay 1 ile 12 arasında olmalı.")

    @classmethod
    def of(cls, day: date) -> YearMonth:
        return cls(day.year, day.month)

    def next(self) -> YearMonth:
        return YearMonth(self.year + self.month // 12, self.month % 12 + 1)

    def previous(self) -> YearMonth:
        return (
            YearMonth(self.year - 1, 12)
            if self.month == 1
            else YearMonth(self.year, self.month - 1)
        )

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return self.next().first_day - timedelta(days=1)

    @property
    def name(self) -> str:
        """`09/2026`"""
        return format_period_tr(self.year, self.month)


def period_amount(annual_amount: Decimal, frequency: Frequency, month: int = 1) -> Decimal:
    """Kalemin `month` ayındaki kesim tutarı (docs/04 §3; Yazılım Dokümanı §8.18 — D-5).

    Yıllık tutar dönemlere `distribute` ile bölünür; kuruş farkı en büyük kalan yöntemiyle ilk
    dönemlere gider, **yıllık toplam her zaman korunur**: 1.000 TL/yıl → Ocak–Nisan 83,34,
    Mayıs–Aralık 83,33 (toplam 1.000,00). Üç aylıkta dilim çeyreğin sırasıdır.
    """
    if frequency is Frequency.MONTHLY:
        return distribute(annual_amount, [Decimal(1)] * 12)[month - 1]
    if frequency is Frequency.QUARTERLY:
        return distribute(annual_amount, [Decimal(1)] * 4)[(month - 1) // 3]
    return annual_amount


def is_item_due(
    frequency: Frequency, period: YearMonth, *, fiscal_year: int, charged_before: bool
) -> bool:
    """Kalem bu dönemin kesimine girer mi? (karar: Furkan, 30.09.2026 — docs/04 §3)

    - aylık: her ay
    - üç aylık: Ocak, Nisan, Temmuz, Ekim
    - yıllık: projenin mali yılının Ocak'ı
    - tek seferlik: o kalemin geçerli (ters kaydı alınmamış) bir kesimi yoksa — bir kez
    """
    match frequency:
        case Frequency.MONTHLY:
            return True
        case Frequency.QUARTERLY:
            return period.month in QUARTER_MONTHS
        case Frequency.YEARLY:
            return period == YearMonth(fiscal_year, 1)
        case Frequency.ONE_TIME:
            return not charged_before


def next_period(last_valid: YearMonth | None, today: date) -> YearMonth:
    """Sıradaki dönem: son **geçerli** koşunun ayından sonraki ay; hiç yoksa bu ay (§7.2).

    Geçerli koşu = kaydedilmiş ve ters kaydı alınmamış. Ters kayıt koşuları hesaba girmez —
    referans uygulamadaki hata (ters kaydı alınan eylül için 30 Ekim önermek) burada yok.
    """
    return last_valid.next() if last_valid is not None else YearMonth.of(today)


def due_date_for(charge_date: date, due_day: int = DEFAULT_DUE_DAY) -> date:
    """K25: vade = tahakkuk ayının `due_day`'i; tahakkuk tarihinden önceye düşerse sonraki ayın
    aynı günü (ör. kesim 15'i, vade günü 10 → sonraki ayın 10'u)."""
    due = date(charge_date.year, charge_date.month, due_day)
    if due < charge_date:
        nxt = YearMonth.of(charge_date).next()
        due = date(nxt.year, nxt.month, due_day)
    return due


def default_dates(period: YearMonth, due_day: int = DEFAULT_DUE_DAY) -> tuple[date, date]:
    """`charge_date` = ayın 1'i, `due_date` = ayın `due_day`'i (K25, varsayılan 10)."""
    charge_date = period.first_day
    return charge_date, due_date_for(charge_date, due_day)


def charge_description(period: YearMonth, item_names: list[str]) -> str:
    """Borç hareketinin açıklaması: `09/2026 tahakkuku — Aidat` ya da `… — 3 kalem` (§7.1)."""
    label = item_names[0] if len(item_names) == 1 else f"{len(item_names)} kalem"
    return f"{period.name} tahakkuku — {label}"


def reversal_description(period: YearMonth) -> str:
    return f"TERS KAYIT — {period.name} tahakkuku iptali"


def already_posted_message(period: YearMonth, posted_on: date, posted_time: str) -> str:
    return (
        f"{period.name} dönemi için tahakkuk zaten kesilmiş ({format_date_tr(posted_on)} "
        f"{posted_time}). Aynı dönem ikinci kez kesilemez; düzeltme gerekiyorsa önce ters "
        "kayıt alın."
    )
