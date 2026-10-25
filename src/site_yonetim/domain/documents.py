"""Belge arşivi — saf kurallar (Yazılım Dokümanı RAP-03, 18.2; frontend servis isteği 78).

- Kapsam: tüm site · blok · bölüm. Gizlilik: sakinlere açık · bölüme özel (bölüm kapsamı zorunlu)
  · yönetim · gizli (yalnız gizli belge yetkisi; her açılış kayda geçer).
- Sürüm: aynı ad (Türkçe harf duyarsız, boşluklar tekleştirilir) + aynı kapsam → yeni sürüm;
  eski sürümler korunur. Son sürümle aynı SHA-256 → reddedilir.
- Dosya: türe uzantıdan değil içerikten karar verilir (PDF, PNG, JPEG, WebP), en fazla 10 MB.
  **Virüs taraması yok** (ilk sürüm); yanıtta ve ekranda "tarandı" denmez.
- Geçerlilik: bitişe 60 ve 30 gün kala uyarı, bitişten sonra "süresi doldu".
- Saklama süresi (K8): belge başına yıl; bilgi amaçlıdır, otomatik silme yapılmaz.

Tarih parametre olarak gelir (domain saati okumaz).
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from site_yonetim.domain.files import (
    MAX_FILE_BYTES,
    FileKind,
    clean_file_name,
    kind_from_content,
    name_with_extension,
)
from site_yonetim.domain.text import tr_lower


class DocumentKind(StrEnum):
    PLAN = "plan"  # yönetim planı
    RULES = "rules"  # site kuralları
    INSURANCE = "insurance"  # sigorta poliçesi
    FIRE_REPORT = "fire_report"  # yangın raporu
    ELEVATOR = "elevator"  # asansör etiketi / muayene
    CONTRACT = "contract"
    MINUTES = "minutes"  # tutanak
    FINANCIAL = "financial"  # mali tablo
    OTHER = "other"


KIND_LABELS: dict[DocumentKind, str] = {
    DocumentKind.PLAN: "Yönetim planı",
    DocumentKind.RULES: "Site kuralları",
    DocumentKind.INSURANCE: "Sigorta poliçesi",
    DocumentKind.FIRE_REPORT: "Yangın raporu",
    DocumentKind.ELEVATOR: "Asansör etiketi / muayene",
    DocumentKind.CONTRACT: "Sözleşme",
    DocumentKind.MINUTES: "Tutanak",
    DocumentKind.FINANCIAL: "Mali tablo",
    DocumentKind.OTHER: "Diğer",
}


class DocumentScope(StrEnum):
    SITE = "site"
    BLOCK = "block"
    UNIT = "unit"


class Confidentiality(StrEnum):
    PUBLIC = "public"  # sakinlere açık (kapsamdaki sakinler)
    UNIT = "unit"  # bölüme özel — o bölümün sakinleri
    MANAGEMENT = "management"  # yalnız yönetim (belge okuma izni olan personel)
    # yalnız gizli belge yetkisi; her açılış kayda geçer (parola değil)
    SECRET = "secret"  # noqa: S105  # nosec B105


class ExpiryWarning(StrEnum):
    DAYS_60 = "60"
    DAYS_30 = "30"
    EXPIRED = "expired"


TITLE_MIN, TITLE_MAX = 3, 120
RETENTION_MIN, RETENTION_MAX = 1, 100
WARN_FIRST, WARN_SECOND = 60, 30

FILE_TYPE_MESSAGE = "Yalnız PDF, PNG, JPEG ya da WebP (dosya içeriği doğrulandı)."
FILE_SIZE_MESSAGE = "Dosya en fazla 10 MB."


class DocumentFieldError(Exception):
    """Alan hataları birlikte — 422 `fields`."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("Formda düzeltilmesi gereken alanlar var.")
        self.fields = fields


class DocumentRuleError(Exception):
    """İş kuralı çakışması — 409. `code` makine içindir, mesaj Türkçedir."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def same_file() -> DocumentRuleError:
    return DocumentRuleError("same_file", "Bu dosya son sürümle aynı.")


def title_key(title: str) -> str:
    """Sürüm eşleşmesi anahtarı: `Yönetim  Planı` ve `YÖNETİM PLANI` aynı belgedir."""
    return tr_lower(" ".join(title.split()))


@dataclass(frozen=True, slots=True)
class CheckedFile:
    file_name: str
    kind: FileKind


def check_file(name: str | None, data: bytes) -> CheckedFile:
    """İçerik imzası + boyut. Ad temizlenir; uzantı içerikle uyuşmuyorsa düzeltilir."""
    if not data:
        raise DocumentFieldError({"file": "Dosya seçin."})
    if len(data) > MAX_FILE_BYTES:
        raise DocumentFieldError({"file": FILE_SIZE_MESSAGE})
    kind = kind_from_content(data)
    if kind is None:
        raise DocumentFieldError({"file": FILE_TYPE_MESSAGE})
    return CheckedFile(name_with_extension(clean_file_name(name), kind), kind)


@dataclass(frozen=True, slots=True)
class DocumentInput:
    """Formdan gelen ham değerler (tür kontrolü burada)."""

    title: str | None
    kind: str | None
    scope: str | None
    has_scope_target: bool
    confidentiality: str | None
    retention_years: int | None


@dataclass(frozen=True, slots=True)
class CheckedDocument:
    title: str
    kind: DocumentKind
    scope: DocumentScope
    confidentiality: Confidentiality
    retention_years: int | None


def _enum[E: StrEnum](cls: type[E], value: str | None) -> E | None:
    try:
        return cls(value) if value else None
    except ValueError:
        return None


def check_document(data: DocumentInput) -> CheckedDocument:
    """Alanları birlikte doğrular; hatalar tek seferde `DocumentFieldError`."""
    errors: dict[str, str] = {}
    title = " ".join((data.title or "").split())
    if len(title) < TITLE_MIN:
        errors["title"] = "Belge adını yazın."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"Belge adı en fazla {TITLE_MAX} karakter olabilir."
    kind = _enum(DocumentKind, data.kind)
    if kind is None:
        errors["kind"] = "Tür seçin."
    scope = _enum(DocumentScope, data.scope)
    if scope is None:
        errors["scope"] = "Kapsam seçin."
    elif scope is not DocumentScope.SITE and not data.has_scope_target:
        errors["scope"] = "Blok ya da bölüm seçin."
    confidentiality = _enum(Confidentiality, data.confidentiality)
    if confidentiality is None:
        errors["confidentiality"] = "Gizlilik seçin."
    elif confidentiality is Confidentiality.UNIT and scope is not DocumentScope.UNIT:
        errors["confidentiality"] = "Bölüme özel belge bir bölüme bağlı olmalı."
    retention = data.retention_years
    if retention is not None and not RETENTION_MIN <= retention <= RETENTION_MAX:
        errors["retention_years"] = (
            f"Saklama süresi {RETENTION_MIN}–{RETENTION_MAX} yıl arasında olmalı."
        )
    if errors or kind is None or scope is None or confidentiality is None:
        raise DocumentFieldError(errors)
    return CheckedDocument(title, kind, scope, confidentiality, retention)


def days_left(valid_until: date | None, today: date) -> int | None:
    return None if valid_until is None else (valid_until - today).days


def expiry_warning(left: int | None) -> ExpiryWarning | None:
    """Kalan gün → uyarı: bitti · ≤ 30 · ≤ 60 · yok."""
    if left is None:
        return None
    if left < 0:
        return ExpiryWarning.EXPIRED
    if left <= WARN_SECOND:
        return ExpiryWarning.DAYS_30
    if left <= WARN_FIRST:
        return ExpiryWarning.DAYS_60
    return None


def expiry_message(title: str, warning: ExpiryWarning, left: int) -> str:
    if warning is ExpiryWarning.EXPIRED:
        return f"{title}: geçerlilik süresi doldu; yenisini yükleyin."
    return f"{title}: geçerliliğin bitmesine {left} gün kaldı; yenilemeyi planlayın."
