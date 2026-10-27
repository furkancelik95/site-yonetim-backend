"""Sözleşme performansı — saf kurallar (Yazılım Dokümanı 15.3, SAT-06; servis istekleri 68–70).

- **KPI**: hedef 1–100 (%), ağırlık 1–100 tam sayı, ağırlıkların toplamı 100. Kaynak: denetim
  listesi puanı, devriye tamamlama oranı, görev tamamlama oranı, talep SLA uyumu, sözleşmeye bağlı
  teknik varlıkların yasal muayene sonucu ya da elle giriş. Kaynak modülü kapalıysa ya da o ay veri
  yoksa KPI elle girilir.
- **Puan** = min(100, gerçekleşen / hedef × 100), yarım yukarı tam sayı; toplam skor ağırlıklı
  ortalama (her KPI'nın gerçekleşeni dolu olmalı).
- **Ceza**: `gerçekleşen < hedef × (1 − eşik/100)` ise kesinti; yüzde kesinti aylık bedel
  üzerinden (yarım yukarı kuruş). Toplam kesinti aylık bedeli aşamaz (bedel biliniyorsa).
  Kabul senaryosu (SAT-06 S1): 20.000 TL/ay, hedef 85, gerçekleşen 72, "hedefin %10'undan fazla
  altında → %5" → 1.000,00 TL.
- **Endeksli artış**: artış tarihi = başlangıç + n × (12 | 6 ay), bitişten sonra artış yok. Endeks
  dönemi = artış tarihinden önceki ay. Ortalama = (TÜFE + ÜFE) / 2. Tavan oranı sınırlar. Hesap
  artış tarihinden 30 gün önce açılır. Bütçe etkisi = (yeni − eski) × yıllık dönem sayısı.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from site_yonetim.domain.facility import add_months
from site_yonetim.domain.management import ContractPeriod
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.text import format_money_tr

HUNDRED = Decimal(100)
RATE_STEP = Decimal("0.01")
DUE_DAYS = 30  # artış hesabı tarihten bu kadar gün önce açılır
LATE_DAYS = 30  # tarihi geçmiş, karara bağlanmamış artış bu kadar gün daha gösterilir
KPIS_MAX = 12
PENALTIES_MAX = 24
NAME_MIN, NAME_MAX = 2, 60
NOTE_MIN, NOTE_MAX = 5, 300
COMMENT_MAX = 500
RATE_MAX = Decimal(200)
INDEX_MIN, INDEX_MAX = Decimal(-50), Decimal(500)


class KpiSource(StrEnum):
    INSPECTION = "inspection"  # denetim listesi puanı (OPS-03): ayın denetim skorları ortalaması
    PATROL = "patrol"  # devriye tamamlama oranı (GUV-09): okutulan / plandaki nokta
    TASKS = "tasks"  # görevli görev tamamlama oranı (OPS-06)
    SLA = "sla"  # talep SLA uyumu (HIZ): ayda çözülenlerden süresinde olanların oranı
    ASSET_INSPECTION = "asset_inspection"  # sözleşmeye bağlı varlıkların muayenesinde olumlu oranı
    MANUAL = "manual"  # elle giriş


SOURCE_LABELS: dict[KpiSource, str] = {
    KpiSource.INSPECTION: "Denetim listesi puanı",
    KpiSource.PATROL: "Devriye tamamlama oranı",
    KpiSource.TASKS: "Görev tamamlama oranı",
    KpiSource.SLA: "Talep SLA uyumu",
    KpiSource.ASSET_INSPECTION: "Teknik varlık muayenesi",
    KpiSource.MANUAL: "Elle giriş",
}


class CutKind(StrEnum):
    PERCENT = "percent"  # aylık bedelden %
    FIXED = "fixed"  # sabit TL


class EscalationKind(StrEnum):
    NONE = "none"
    TUFE = "tufe"
    UFE = "ufe"
    AVG = "avg"  # (TÜFE + ÜFE) / 2
    FIXED = "fixed"


class EscalationPeriod(StrEnum):
    YEARLY = "yearly"
    SEMIANNUAL = "semiannual"


class ScorecardStatus(StrEnum):
    DRAFT = "draft"
    FINALIZED = "finalized"


class PenaltyStatus(StrEnum):
    """Kayıtlı kesinti. API'de ayrıca `none`: taslak karnede hesaplanan, henüz onaya düşmemiş."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EscalationStatus(StrEnum):
    PENDING = "pending"  # onay mekanizmasında bekliyor
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"  # onay talebi geri çekildi; yeniden karar verilebilir


class AmountSource(StrEnum):
    INITIAL = "initial"  # sözleşme kaydı
    EDIT = "edit"  # sözleşme formunda değişiklik
    ESCALATION = "escalation"  # onaylı endeksli / sabit artış


class PreviewStatus(StrEnum):
    NONE = "none"  # artış kuralı yok, bedel yok ya da bitişe kadar artış yok
    NOT_DUE = "not_due"  # artış tarihine 30 günden fazla var
    MISSING_INDEX = "missing_index"  # endeks dönemi değeri girilmemiş
    DUE = "due"  # karar bekliyor
    PENDING = "pending"  # onay mekanizmasında
    APPROVED = "approved"
    REJECTED = "rejected"


# --- Tanımların doğrulanması ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KpiInput:
    key: str  # mevcut KPI kimliği ya da istemcinin verdiği geçici kimlik
    name: str | None
    source: str | None
    target: Decimal | None
    weight: int | None
    request_category: str | None


@dataclass(frozen=True, slots=True)
class PenaltyInput:
    kpi_key: str | None
    below_percent: Decimal | None
    cut_kind: str | None
    cut_value: Decimal | None


@dataclass(frozen=True, slots=True)
class EscalationInput:
    kind: str | None
    fixed_rate: Decimal | None
    period: str | None
    cap: Decimal | None


@dataclass(frozen=True, slots=True)
class Kpi:
    key: str
    name: str
    source: KpiSource
    target: Decimal
    weight: int
    request_category: str | None


@dataclass(frozen=True, slots=True)
class PenaltyRule:
    kpi_key: str
    below_percent: Decimal
    cut_kind: CutKind
    cut_value: Decimal


@dataclass(frozen=True, slots=True)
class Escalation:
    kind: EscalationKind
    fixed_rate: Decimal | None
    period: EscalationPeriod
    cap: Decimal | None


@dataclass(frozen=True, slots=True)
class Definitions:
    kpis: list[Kpi]
    penalties: list[PenaltyRule]
    escalation: Escalation


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _two_places(value: Decimal) -> bool:
    return value == value.quantize(RATE_STEP)


def check_definitions(
    kpis: Sequence[KpiInput],
    penalties: Sequence[PenaltyInput],
    escalation: EscalationInput,
    *,
    has_amount: bool,
    has_monthly_amount: bool,
    request_categories: set[str],
) -> tuple[Definitions, dict[str, str]]:
    """KPI, ceza ve artış tanımlarını doğrular; (temiz tanımlar, alan hataları) döner."""
    errors: dict[str, str] = {}
    clean_kpis: list[Kpi] = []
    if len(kpis) > KPIS_MAX:
        errors["kpis"] = f"En çok {KPIS_MAX} KPI tanımlanabilir."
    for i, k in enumerate(kpis):
        name = _clean(k.name)
        bad = False
        if not NAME_MIN <= len(name) <= NAME_MAX:
            errors[f"kpis.{i}.name"] = "KPI adı yazın (2–60 karakter)."
            bad = True
        if k.source not in {s.value for s in KpiSource}:
            errors[f"kpis.{i}.source"] = "Kaynak seçin."
            bad = True
        if k.target is None or not (ZERO < k.target <= HUNDRED) or not _two_places(k.target):
            errors[f"kpis.{i}.target"] = "Hedef 1–100 (%) olmalı."
            bad = True
        if k.weight is None or not 1 <= k.weight <= 100:
            errors[f"kpis.{i}.weight"] = "Ağırlık 1–100 olmalı."
            bad = True
        category = _clean(k.request_category) or None
        if k.source == KpiSource.SLA.value and category and category not in request_categories:
            errors[f"kpis.{i}.request_category"] = "Talep kategorisi bulunamadı."
            bad = True
        if not bad and k.target is not None and k.weight is not None and k.source is not None:
            clean_kpis.append(
                Kpi(
                    key=k.key,
                    name=name,
                    source=KpiSource(k.source),
                    target=k.target,
                    weight=k.weight,
                    request_category=category if k.source == KpiSource.SLA.value else None,
                )
            )
    if kpis and "kpis" not in errors and sum(k.weight or 0 for k in kpis) != 100:
        errors["kpis"] = "Ağırlıkların toplamı 100 olmalı."
    keys = [k.key for k in kpis]
    if len(set(keys)) != len(keys):
        errors["kpis"] = "KPI kimlikleri tekrar ediyor."

    clean_penalties: list[PenaltyRule] = []
    if len(penalties) > PENALTIES_MAX:
        errors["penalties"] = f"En çok {PENALTIES_MAX} ceza kuralı tanımlanabilir."
    for i, p in enumerate(penalties):
        bad = False
        if p.kpi_key is None or p.kpi_key not in keys:
            errors[f"penalties.{i}.kpi_id"] = "KPI seçin."
            bad = True
        if (
            p.below_percent is None
            or not (ZERO <= p.below_percent < HUNDRED)
            or not _two_places(p.below_percent)
        ):
            errors[f"penalties.{i}.below_percent"] = "Eşik %0–99 olmalı."
            bad = True
        if p.cut_kind not in {c.value for c in CutKind}:
            errors[f"penalties.{i}.cut_kind"] = "Kesinti tipini seçin."
            bad = True
        elif p.cut_value is None or p.cut_value <= ZERO or not _two_places(p.cut_value):
            errors[f"penalties.{i}.cut_value"] = (
                "Kesinti %1–100 olmalı." if p.cut_kind == CutKind.PERCENT.value else "Tutar > 0."
            )
            bad = True
        elif p.cut_kind == CutKind.PERCENT.value and not (1 <= p.cut_value <= HUNDRED):
            errors[f"penalties.{i}.cut_value"] = "Kesinti %1–100 olmalı."
            bad = True
        if not bad and p.kpi_key and p.below_percent is not None and p.cut_value is not None:
            clean_penalties.append(
                PenaltyRule(p.kpi_key, p.below_percent, CutKind(str(p.cut_kind)), p.cut_value)
            )
    if (
        any(p.cut_kind == CutKind.PERCENT.value for p in penalties)
        and not has_monthly_amount
        and "penalties" not in errors
    ):
        errors["penalties"] = "Yüzde kesinti için sözleşmede aylık ya da yıllık bedel olmalı."

    kind = escalation.kind or EscalationKind.NONE.value
    period = escalation.period or EscalationPeriod.YEARLY.value
    if kind not in {k.value for k in EscalationKind}:
        errors["escalation.kind"] = "Artış kuralı seçin."
    elif kind != EscalationKind.NONE.value and not has_amount:
        errors["escalation.kind"] = "Artış için sözleşmede bedel olmalı."
    if period not in {p.value for p in EscalationPeriod}:
        errors["escalation.period"] = "Artış dönemini seçin."
    rate = escalation.fixed_rate
    if kind == EscalationKind.FIXED.value and (
        rate is None or not (RATE_STEP <= rate <= RATE_MAX) or not _two_places(rate)
    ):
        errors["escalation.fixed_rate"] = "Oran %0,01–200 olmalı."
    cap = escalation.cap
    if cap is not None and (cap <= ZERO or cap > RATE_MAX or not _two_places(cap)):
        errors["escalation.cap"] = "Tavan boş ya da %0,01–200 olmalı."
    clean_escalation = Escalation(
        kind=EscalationKind(kind)
        if kind in {k.value for k in EscalationKind}
        else (EscalationKind.NONE),
        fixed_rate=rate if kind == EscalationKind.FIXED.value else None,
        period=EscalationPeriod(period)
        if period in {p.value for p in EscalationPeriod}
        else EscalationPeriod.YEARLY,
        cap=cap if kind != EscalationKind.NONE.value else None,
    )
    return Definitions(clean_kpis, clean_penalties, clean_escalation), errors


# --- Bedel -------------------------------------------------------------------------------


def monthly_amount(amount: Decimal | None, period: str | None) -> Decimal | None:
    """Aylık bedel: aylık sözleşmede bedel, yıllıkta bedel / 12 (yarım yukarı); tek seferlikte
    ya da bedelsizde yok."""
    if amount is None:
        return None
    if period == ContractPeriod.MONTHLY.value:
        return amount
    if period == ContractPeriod.YEARLY.value:
        return round_money(amount / 12)
    return None


def periods_per_year(period: str | None) -> int:
    if period == ContractPeriod.MONTHLY.value:
        return 12
    if period == ContractPeriod.YEARLY.value:
        return 1
    return 0


# --- Karne ----------------------------------------------------------------------------------


def percent(part: int, whole: int) -> Decimal | None:
    """Oran (%), yarım yukarı tam sayı; payda 0 ise None."""
    if whole <= 0:
        return None
    return (Decimal(part * 100) / Decimal(whole)).quantize(Decimal(1), ROUND_HALF_UP)


def kpi_score(actual: Decimal | None, target: Decimal) -> int | None:
    if actual is None:
        return None
    raw = actual / target * HUNDRED
    return int(min(HUNDRED, raw).quantize(Decimal(1), ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class ScoreRow:
    kpi_key: str
    name: str
    source: KpiSource
    target: Decimal
    weight: int
    actual: Decimal | None
    detail: str | None

    @property
    def score(self) -> int | None:
        return kpi_score(self.actual, self.target)


def total_score(rows: Sequence[ScoreRow]) -> int | None:
    """Ağırlıklı ortalama (yarım yukarı); bir KPI eksikse ya da KPI yoksa None."""
    weights = sum(r.weight for r in rows)
    scores = [r.score for r in rows]
    if not rows or weights == 0 or any(s is None for s in scores):
        return None
    total = sum((Decimal(s or 0) * r.weight for s, r in zip(scores, rows, strict=True)), Decimal(0))
    return int((total / weights).quantize(Decimal(1), ROUND_HALF_UP))


def number_tr(value: Decimal) -> str:
    """85 → "85", 76.5 → "76,5" (gereksiz sıfırsız)."""
    text = f"{value.normalize():f}"
    return text.replace(".", ",")


@dataclass(frozen=True, slots=True)
class PenaltyResult:
    amount: Decimal
    reasons: list[str]


def compute_penalty(
    rows: Sequence[ScoreRow], rules: Sequence[PenaltyRule], monthly: Decimal | None
) -> PenaltyResult | None:
    """Tetiklenen kurallar ve toplam kesinti; tetiklenen yoksa None."""
    by_key = {r.kpi_key: r for r in rows}
    amount = ZERO
    reasons: list[str] = []
    for rule in rules:
        row = by_key.get(rule.kpi_key)
        if row is None or row.actual is None:
            continue
        limit = row.target * (1 - rule.below_percent / HUNDRED)
        if row.actual >= limit:
            continue
        if rule.cut_kind is CutKind.PERCENT:
            if monthly is None:
                continue
            cut = round_money(monthly * rule.cut_value / HUNDRED)
            cut_text = f"%{number_tr(rule.cut_value)}"
        else:
            cut = rule.cut_value
            cut_text = format_money_tr(rule.cut_value)
        amount += cut
        shown_limit = limit.quantize(Decimal("0.1"), ROUND_HALF_UP)
        reasons.append(
            f"{row.name}: gerçekleşen {number_tr(row.actual)}, hedef {number_tr(row.target)} "
            f"(sınır {number_tr(shown_limit)}) → {cut_text} kesinti"
        )
    if not reasons:
        return None
    if monthly is not None and amount > monthly:
        amount = monthly
    return PenaltyResult(round_money(amount), reasons)


# --- Endeksli artış -----------------------------------------------------------------------


def step_months(period: EscalationPeriod) -> int:
    return 12 if period is EscalationPeriod.YEARLY else 6


def escalation_date(start: date, end: date, period: EscalationPeriod, today: date) -> date | None:
    """Gösterilecek artış tarihi: başlangıç + n × adım (n ≥ 1) içinden `bugün − 30 gün`den
    sonraki ilki (tarihi geçmiş ama karara bağlanmamış artış 30 gün daha görünür). Bitişten
    sonraysa None."""
    step = step_months(period)
    n = 1
    while True:
        day = add_months(start, n * step)
        if day > end:
            return None
        if day >= today - timedelta(days=LATE_DAYS):
            return day
        n += 1


def index_period(day: date) -> str:
    """Artış tarihinden önceki ay: 2026-09-01 → "2026-08"."""
    prev = day.replace(day=1) - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def index_rate(kind: EscalationKind, tufe: Decimal | None, ufe: Decimal | None) -> Decimal | None:
    if kind is EscalationKind.TUFE:
        return tufe
    if kind is EscalationKind.UFE:
        return ufe
    if kind is EscalationKind.AVG and tufe is not None and ufe is not None:
        return ((tufe + ufe) / 2).quantize(RATE_STEP, ROUND_HALF_UP)
    return None


def capped(rate: Decimal, cap: Decimal | None) -> Decimal:
    return min(rate, cap) if cap is not None else rate


def escalated_amount(old: Decimal, rate: Decimal) -> Decimal:
    return round_money(old * (HUNDRED + rate) / HUNDRED)


def annual_effect(old: Decimal, new: Decimal, period: str | None) -> Decimal:
    return round_money((new - old) * periods_per_year(period))


def check_index(
    period: str, tufe: Decimal | None, ufe: Decimal | None, *, this_month: str
) -> dict[str, str]:
    errors: dict[str, str] = {}
    valid = (
        len(period) == 7
        and period[4] == "-"
        and (period[:4] + period[5:]).isdigit()
        and 1 <= int(period[5:]) <= 12
        and int(period[:4]) >= 2000
    )
    if not valid or period > this_month:
        errors["period"] = "Dönem geçmiş ya da bu ay olmalı (YYYY-AA)."
    if tufe is None or not (INDEX_MIN < tufe < INDEX_MAX) or not _two_places(tufe):
        errors["tufe"] = "Yıllık TÜFE (%) girin; en çok 2 ondalık."
    if ufe is None or not (INDEX_MIN < ufe < INDEX_MAX) or not _two_places(ufe):
        errors["ufe"] = "Yıllık ÜFE (%) girin; en çok 2 ondalık."
    return errors


def clean_note(value: str | None) -> str | None:
    text = _clean(value)[:NOTE_MAX]
    return text or None


def period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"
