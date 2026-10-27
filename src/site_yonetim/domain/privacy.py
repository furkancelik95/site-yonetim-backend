"""KVKK — saf kurallar (Yazılım Dokümanı RAP-04, §18.2; PLT-01 adım 14; PM kararları P6, K8).

- **Aydınlatma metni** site başına sürümlüdür (Türkçe bağlayıcı metin, en az 200 karakter);
  eski sürümler saklanır, değişmez. P6: kullanıcı sitenin **son** sürümünü onaylamadan oturumda
  yalnız `/me`, `/auth/*`, onay uçları ve metnin kendisi çalışır; yeni sürüm herkesten yeniden
  onay ister.
- **Açık rıza** aydınlatmadan ayrıdır, isteğe bağlıdır ve hizmet şartına bağlanmaz; amaç bazında
  verilir/geri çekilir (her değişiklik yeni satır, en sonuncusu geçerli).
- **İlgili kişi başvurusu** (KVKK m.11, m.13): erişim / düzeltme / silme / itiraz; başvuru anında
  30 gün sayacı başlar. Aynı türde açık başvuru varken yenisi açılmaz. Silmede sonuç "yerine
  getirildi" değilse saklanan kayıtlar ve yasal dayanağı yazılır.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

RESPONSE_DAYS = 30  # KVKK m.13/2
WARN_DAYS = 7  # son 7 günde uyarı (servis isteği 80)

NOTICE_TEXT_MIN = 200
NOTICE_TEXT_MAX = 50_000
NOTICE_SUMMARY_MIN = 5
NOTICE_SUMMARY_MAX = 200
DETAIL_MIN = 10
DETAIL_MAX = 1000
RESPONSE_MIN = 10
RESPONSE_MAX = 2000
RETAINED_MAX = 1000


class PrivacyRequestKind(StrEnum):
    ACCESS = "access"
    RECTIFY = "rectify"
    ERASE = "erase"
    OBJECT = "object"


KIND_LABELS: dict[PrivacyRequestKind, str] = {
    PrivacyRequestKind.ACCESS: "Verilerime erişim",
    PrivacyRequestKind.RECTIFY: "Düzeltme",
    PrivacyRequestKind.ERASE: "Silme",
    PrivacyRequestKind.OBJECT: "İtiraz",
}


class PrivacyRequestStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


class PrivacyOutcome(StrEnum):
    FULFILLED = "fulfilled"
    PARTIAL = "partial"
    REJECTED = "rejected"


OUTCOME_LABELS: dict[PrivacyOutcome, str] = {
    PrivacyOutcome.FULFILLED: "Yerine getirildi",
    PrivacyOutcome.PARTIAL: "Kısmen yerine getirildi",
    PrivacyOutcome.REJECTED: "Reddedildi",
}


class ConsentSource(StrEnum):
    """Onayın nereden geldiği (kanıt için saklanır)."""

    LOGIN = "login"  # girişten sonraki onay ekranı (PLT-01 adım 15)
    REGISTRATION = "registration"  # kayıt başvurusu formu (onayda hesaba bağlanır)
    INVITATION = "invitation"  # davet kabul formu
    PUBLISHER = "publisher"  # sürümü yayınlayan yönetici
    SEED = "seed"  # demo verisi


class ExplicitPurpose(StrEnum):
    """Açık rıza amaçları (aydınlatmadan ayrı; isteğe bağlı)."""

    COMMUNICATIONS = "communications"


PURPOSE_LABELS: dict[ExplicitPurpose, str] = {
    ExplicitPurpose.COMMUNICATIONS: ("Duyuru ve bilgilendirmelerin SMS ve e-postayla gönderilmesi"),
}


class PrivacyFieldError(ValueError):
    """Alan hataları — 422 `fields`."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


def _clean(value: str | None) -> str:
    return " ".join((value or "").split()) if value else ""


def _clean_text(value: str | None) -> str:
    """Çok satırlı metin: satır sonları korunur, baş/son boşluk atılır."""
    return (value or "").strip()


@dataclass(frozen=True, slots=True)
class CheckedRequest:
    kind: PrivacyRequestKind
    detail: str


def check_request(kind: str | None, detail: str | None) -> CheckedRequest:
    errors: dict[str, str] = {}
    try:
        parsed = PrivacyRequestKind(kind or "")
    except ValueError:
        errors["kind"] = "Başvuru türünü seçin."
        parsed = PrivacyRequestKind.ACCESS
    text = _clean_text(detail)
    if parsed is not PrivacyRequestKind.ACCESS and len(text) < DETAIL_MIN:
        errors["detail"] = "Talebinizi kısaca açıklayın."
    elif len(text) > DETAIL_MAX:
        errors["detail"] = f"Açıklama en fazla {DETAIL_MAX} karakter olabilir."
    if errors:
        raise PrivacyFieldError(errors)
    return CheckedRequest(parsed, text)


@dataclass(frozen=True, slots=True)
class CheckedAnswer:
    response: str
    outcome: PrivacyOutcome
    retained: str | None


def check_answer(
    kind: PrivacyRequestKind, response: str | None, outcome: str | None, retained: str | None
) -> CheckedAnswer:
    errors: dict[str, str] = {}
    text = _clean_text(response)
    if len(text) < RESPONSE_MIN:
        errors["response"] = "Gerekçeli yanıt yazın."
    elif len(text) > RESPONSE_MAX:
        errors["response"] = f"Yanıt en fazla {RESPONSE_MAX} karakter olabilir."
    try:
        parsed = PrivacyOutcome(outcome or "")
    except ValueError:
        errors["outcome"] = "Sonucu seçin."
        parsed = PrivacyOutcome.FULFILLED
    kept = _clean_text(retained) or None
    if (
        "outcome" not in errors
        and kind is PrivacyRequestKind.ERASE
        and parsed is not PrivacyOutcome.FULFILLED
        and not kept
    ):
        errors["retained"] = (
            "Saklanan kayıtları ve yasal dayanağı yazın (ör. finansal kayıtlar 10 yıl)."
        )
    if kept and len(kept) > RETAINED_MAX:
        errors["retained"] = f"En fazla {RETAINED_MAX} karakter."
    if errors:
        raise PrivacyFieldError(errors)
    return CheckedAnswer(text, parsed, kept)


@dataclass(frozen=True, slots=True)
class CheckedNotice:
    text: str
    summary: str


def check_notice(text: str | None, summary: str | None) -> CheckedNotice:
    errors: dict[str, str] = {}
    body = _clean_text(text)
    if len(body) < NOTICE_TEXT_MIN:
        errors["text"] = f"Aydınlatma metni çok kısa (en az {NOTICE_TEXT_MIN} karakter)."
    elif len(body) > NOTICE_TEXT_MAX:
        errors["text"] = f"Aydınlatma metni en fazla {NOTICE_TEXT_MAX} karakter olabilir."
    note = _clean(summary)
    if len(note) < NOTICE_SUMMARY_MIN:
        errors["summary"] = "Değişiklik özetini yazın."
    elif len(note) > NOTICE_SUMMARY_MAX:
        errors["summary"] = f"Özet en fazla {NOTICE_SUMMARY_MAX} karakter olabilir."
    if errors:
        raise PrivacyFieldError(errors)
    return CheckedNotice(body, note)


def due_on(created: date) -> date:
    """Yanıt için son gün: başvuru günü + 30 gün."""
    return created + timedelta(days=RESPONSE_DAYS)


def days_left(due: date, today: date) -> int:
    """Eksi: gecikme günü."""
    return (due - today).days


def needs_warning(due: date, today: date) -> bool:
    """Son 7 gün ve gecikmede günlük uyarı."""
    return days_left(due, today) <= WARN_DAYS
