"""Satın alma — saf kurallar (Yazılım Dokümanı §15, SAT-01..06; Ürün Dokümanı 8.11;
frontend servis istekleri 62–67).

- **Tedarikçi kartı:** unvan 2–120, vergi no 10 hane (şahısta T.C. kimlik 11), site içinde tekil;
  IBAN isteğe bağlı, okumada her zaman maskeli. Pasif ve kara listedeki tedarikçiye teklif linki
  gönderilmez, teklif girilmez, sipariş verilmez.
- **Asgari teklif sayısı** tutar eşiklerine göre (tesis ayarı; varsayılan < 10.000 → 1,
  10.000–100.000 → 2, üstü → 3). Eşik "altındaysa": `tutar < eşik`.
- **Üçlü eşleşme:** her kalemde faturadaki miktar = teslim alınan ve henüz faturalanmamış miktar
  (tolerans yok); toplam, `beklenen = Σ miktar × sipariş birim fiyatı` ile tolerans (tesis ayarı,
  varsayılan %1) içinde olmalı. Toplam tolerans içindeyse birim fiyat sapması eşleşmeyi bozmaz.
- Para `Decimal`, kalem tutarı `round_money(miktar × birim fiyat)` (yarım yukarı).

Tarih ve saat parametre olarak gelir (domain saati okumaz).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.text import format_money_tr
from site_yonetim.domain.validation import normalize_email_address, normalize_iban


class VendorStatus(StrEnum):
    ACTIVE = "active"
    PASSIVE = "passive"
    BLACKLIST = "blacklist"


class PurchaseStatus(StrEnum):
    QUOTING = "quoting"  # teklif toplanıyor
    EXCEPTION_PENDING = "exception_pending"  # eksik teklif → teklif istisnası onayı
    APPROVAL_PENDING = "approval_pending"  # satın alma onayı
    APPROVED = "approved"  # sipariş oluşturulabilir
    REJECTED = "rejected"
    ORDERED = "ordered"
    CANCELLED = "cancelled"


OPEN_PURCHASE = frozenset(
    {
        PurchaseStatus.QUOTING,
        PurchaseStatus.EXCEPTION_PENDING,
        PurchaseStatus.APPROVAL_PENDING,
        PurchaseStatus.APPROVED,
    }
)
# Bütçede "açık taahhüt" sayılan talepler (servis isteği 63).
COMMITTED_PURCHASE = frozenset(
    {
        PurchaseStatus.EXCEPTION_PENDING,
        PurchaseStatus.APPROVAL_PENDING,
        PurchaseStatus.APPROVED,
    }
)


class QuoteSource(StrEnum):
    STAFF = "staff"  # personel girdi
    VENDOR_LINK = "vendor_link"  # tedarikçi linkten girdi


class DecisionStage(StrEnum):
    EXCEPTION = "exception"  # teklif istisnası
    PURCHASE = "purchase"  # satın alma onayı
    ORDER = "order"  # sipariş onayı (onay kuralı varsa)


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    WITHDRAW = "withdraw"  # onay talebi geri çekildi


class OrderStatus(StrEnum):
    SENT = "sent"
    PARTIAL = "partial"
    RECEIVED = "received"
    CLOSED = "closed"


class InvoiceStatus(StrEnum):
    MATCHED = "matched"
    DIFF_PENDING = "diff_pending"
    DISPUTED = "disputed"
    PAID = "paid"


class LinkState(StrEnum):
    OPEN = "open"
    USED = "used"
    EXPIRED = "expired"  # süresi doldu ya da talep teklif aşamasından çıktı


# --- Hata --------------------------------------------------------------------------------


class PurchaseFieldError(Exception):
    """Alan hataları → 422 `validation_error` + `fields`."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


class PurchaseRuleError(Exception):
    """İş kuralı ihlali: `status` (403/404/409/410/422), `code` makine için, mesaj Türkçe."""

    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


# --- Sınırlar ----------------------------------------------------------------------------

NAME_MIN, NAME_MAX = 2, 120
CATEGORY_MAX, NOTE_MAX = 60, 500
TITLE_MIN, TITLE_MAX = 3, 120
REASON_MIN, REASON_MAX = 5, 500
ITEM_MAX = 120
UNIT_MAX = 12
ITEMS_MAX = 50
EXCEPTION_REASON_MIN = 10
REJECT_NOTE_MIN = 5
DECISION_NOTE_MAX = 500
PAYMENT_TERMS_MIN, PAYMENT_TERMS_MAX = 2, 80
DELIVERY_DAYS_MAX = 365
INVOICE_NUMBER_MIN, INVOICE_NUMBER_MAX = 3, 30
RATING_MIN, RATING_MAX = 1, 5
QUOTES_MIN, QUOTES_MAX = 1, 10
TOLERANCE_MAX = Decimal(10)
LINK_DAYS_MIN, LINK_DAYS_MAX = 1, 30
THRESHOLDS_MAX = 6
QUANTITY_PLACES = 3
QUANTITY_MAX = Decimal("99999999.999")
LINE_PRICE_MAX = Decimal("100000000.00")


@dataclass(frozen=True, slots=True)
class Threshold:
    max: Decimal  # bu tutarın altındaysa
    quotes: int


@dataclass(frozen=True, slots=True)
class Settings:
    thresholds: tuple[Threshold, ...]
    above_quotes: int
    tolerance_percent: Decimal
    link_days: int


DEFAULT_SETTINGS = Settings(
    thresholds=(Threshold(Decimal("10000.00"), 1), Threshold(Decimal("100000.00"), 2)),
    above_quotes=3,
    tolerance_percent=Decimal(1),
    link_days=7,
)


def clean(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


# --- Tedarikçi -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VendorInput:
    name: str | None
    tax_no: str | None
    phone: str | None
    email: str | None
    iban: str | None  # None/boş → (düzenlemede) mevcut korunur
    category: str | None
    status: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CheckedVendor:
    name: str
    tax_number: str
    phone: str | None
    email: str | None
    iban: str | None  # None → değişmez (düzenleme) / yok (yeni)
    category: str | None
    status: VendorStatus
    note: str | None


def normalize_phone(value: str) -> str:
    """Sabit ya da cep: 10 hane (baştaki 0/90 atılır) → `+90…`."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 12 and digits.startswith("90"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits.startswith("0"):
        raise ValueError("Telefon 10 hane olmalı (ör. 0212 123 45 67).")
    return f"+90{digits}"


def check_vendor(data: VendorInput) -> CheckedVendor:
    errors: dict[str, str] = {}
    name = clean(data.name, 200)
    if not NAME_MIN <= len(name) <= NAME_MAX:
        errors["name"] = f"Unvan yazın ({NAME_MIN}–{NAME_MAX} karakter)."
    tax = "".join((data.tax_no or "").split())
    if not (tax.isdigit() and len(tax) in (10, 11)):
        errors["tax_no"] = "Vergi no 10, T.C. kimlik no 11 hane olmalı."
    phone = None
    if data.phone and data.phone.strip():
        try:
            phone = normalize_phone(data.phone)
        except ValueError as exc:
            errors["phone"] = str(exc)
    email = None
    if data.email and data.email.strip():
        try:
            email = normalize_email_address(data.email)
        except ValueError as exc:
            errors["email"] = str(exc)
    iban = None
    if data.iban and data.iban.strip():
        try:
            iban = normalize_iban(data.iban)
        except ValueError as exc:
            errors["iban"] = str(exc)
    status: VendorStatus | None = VendorStatus.ACTIVE
    if data.status is not None:
        try:
            status = VendorStatus(data.status)
        except ValueError:
            errors["status"] = "Durumu seçin."
    if errors or status is None:
        raise PurchaseFieldError(errors)
    return CheckedVendor(
        name=name,
        tax_number=tax,
        phone=phone,
        email=email,
        iban=iban,
        category=clean(data.category, CATEGORY_MAX) or None,
        status=status,
        note=clean(data.note, NOTE_MAX) or None,
    )


def mask_iban_display(iban: str | None) -> str | None:
    """mask_iban_display("TR330006100519786457841326") → "TR33 •••• •••• 1326" """
    if not iban:
        return None
    return f"{iban[:4]} •••• •••• {iban[-4:]}"


# --- Ayarlar -------------------------------------------------------------------------------


def check_settings(
    thresholds: Sequence[tuple[Decimal, int]],
    above_quotes: int,
    tolerance_percent: Decimal,
    link_days: int,
) -> Settings:
    errors: dict[str, str] = {}
    if not thresholds or len(thresholds) > THRESHOLDS_MAX:
        errors["thresholds"] = f"1–{THRESHOLDS_MAX} eşik tanımlayın."
    elif any(m <= 0 or not QUOTES_MIN <= q <= QUOTES_MAX for m, q in thresholds):
        errors["thresholds"] = (
            f"Her eşik için tutar sıfırdan büyük ve teklif sayısı {QUOTES_MIN}–{QUOTES_MAX} olmalı."
        )
    elif any(thresholds[i][0] <= thresholds[i - 1][0] for i in range(1, len(thresholds))):
        errors["thresholds"] = "Eşikler küçükten büyüğe sıralanmalı."
    if not QUOTES_MIN <= above_quotes <= QUOTES_MAX:
        errors["above_quotes"] = f"Teklif sayısı {QUOTES_MIN}–{QUOTES_MAX} olmalı."
    if (
        not ZERO <= tolerance_percent <= TOLERANCE_MAX
        or tolerance_percent != tolerance_percent.quantize(Decimal("0.01"))
    ):
        errors["tolerance_percent"] = "Tolerans %0–10 olmalı (en çok 2 ondalık)."
    if not LINK_DAYS_MIN <= link_days <= LINK_DAYS_MAX:
        errors["link_days"] = f"Link geçerliliği {LINK_DAYS_MIN}–{LINK_DAYS_MAX} gün olmalı."
    if errors:
        raise PurchaseFieldError(errors)
    return Settings(
        thresholds=tuple(Threshold(round_money(m), q) for m, q in thresholds),
        above_quotes=above_quotes,
        tolerance_percent=tolerance_percent,
        link_days=link_days,
    )


def required_quotes(settings: Settings, total: Decimal) -> int:
    """Tutar ilk eşiğin altındaysa o eşiğin sayısı; hiçbirinin altında değilse `above_quotes`."""
    for threshold in sorted(settings.thresholds, key=lambda t: t.max):
        if total < threshold.max:
            return threshold.quotes
    return settings.above_quotes


# --- Miktar ve tutar -----------------------------------------------------------------------


def parse_quantity(value: object, *, allow_zero: bool) -> Decimal:
    """Sayı ya da metin → en çok 3 ondalık; negatif yok. Geçersizse `ValueError`."""
    if isinstance(value, bool) or value is None:
        raise ValueError("miktar")
    text = str(value).strip().replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(text) from exc
    exponent = number.as_tuple().exponent
    if (
        not number.is_finite()
        or (isinstance(exponent, int) and exponent < -QUANTITY_PLACES)
        or number < 0
        or (number == 0 and not allow_zero)
        or number > QUANTITY_MAX
    ):
        raise ValueError(text)
    return number.quantize(Decimal(1).scaleb(-QUANTITY_PLACES))


def line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return round_money(quantity * unit_price)


def format_quantity(value: Decimal) -> str:
    """format_quantity(Decimal("4.500")) → "4,5" (metin içinde)."""
    text = f"{value.normalize():f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def request_number(year: int, sequence: int) -> str:
    """request_number(2026, 1) → "ST-2026-0001" """
    return f"ST-{year}-{sequence:04d}"


def order_number(year: int, sequence: int) -> str:
    """order_number(2026, 1) → "SIP-2026-0001" """
    return f"SIP-{year}-{sequence:04d}"


# --- Bütçe (servis isteği 63) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    has_budget: bool
    annual: Decimal
    spent: Decimal
    committed: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.annual - self.spent - self.committed

    def exceeded_by(self, amount: Decimal) -> bool:
        """Kategoride bütçe kalemi yoksa aşım işaretlenmez."""
        return self.has_budget and amount > self.remaining


# --- Üçlü eşleşme (servis isteği 66) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderLineView:
    description: str
    unit: str
    unit_price: Decimal
    open_quantity: Decimal  # teslim alınmış, henüz faturalanmamış


@dataclass(frozen=True, slots=True)
class InvoiceLineInput:
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class MatchResult:
    total: Decimal
    expected: Decimal
    diffs: list[str]
    matched: bool


def tolerance_amount(expected: Decimal, percent: Decimal) -> Decimal:
    return round_money(expected * percent / 100)


def match_invoice(
    order: Sequence[OrderLineView], invoice: Sequence[InvoiceLineInput], tolerance_percent: Decimal
) -> MatchResult:
    """Sipariş + kabul + fatura karşılaştırması. Miktar farkı ya da tolerans dışı toplam →
    eşleşmez (fark onaya düşer). Eşleşmeyen faturada birim fiyat farkları da listelenir."""
    quantity_diffs: list[str] = []
    price_diffs: list[str] = []
    for line, inv in zip(order, invoice, strict=True):
        if inv.quantity != line.open_quantity:
            quantity_diffs.append(
                f"{line.description}: faturada {format_quantity(inv.quantity)}, teslim alınan "
                f"{format_quantity(line.open_quantity)} {line.unit}"
            )
        if inv.quantity > 0 and inv.unit_price != line.unit_price:
            price_diffs.append(
                f"{line.description}: birim fiyat {format_money_tr(inv.unit_price)}, siparişte "
                f"{format_money_tr(line.unit_price)}"
            )
    total = sum((line_total(i.quantity, i.unit_price) for i in invoice), ZERO)
    expected = sum((line_total(o.open_quantity, o.unit_price) for o in order), ZERO)
    tolerance_ok = abs(total - expected) <= tolerance_amount(expected, tolerance_percent)
    matched = not quantity_diffs and tolerance_ok
    diffs = list(quantity_diffs)
    if not matched:
        diffs.extend(price_diffs)
    if not tolerance_ok:
        diffs.append(
            f"Toplam {format_money_tr(total)}, beklenen {format_money_tr(expected)} "
            f"(tolerans %{format_quantity(tolerance_percent)})"
        )
    return MatchResult(total=total, expected=expected, diffs=diffs, matched=matched)
