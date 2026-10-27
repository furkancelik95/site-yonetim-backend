"""Özel alanlar — saf kurallar (servis isteği 24, Yazılım Dokümanı YAP-10).

Tesis kod yazmadan bölüm ve kişi kartına alan ekler. Anahtar etiketten bir kez üretilir ve
değişmez (Excel ve API'de kullanılır); tip sonradan değişmez. Özel nitelikli kişisel veri
(KVKK m.6) çağrıştıran etiketler uyarıyla durdurulur; gerekçeyle geçilebilir (denetim kaydı).
"""

import numbers
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from site_yonetim.domain.text import ascii_fold, tr_lower

FIELD_LIMIT = 30  # tesis başına nesne türünde etkin alan
LABEL_MAX = 60
OPTIONS_MIN, OPTIONS_MAX = 2, 50
TEXT_MAX = 500
KEY_MAX = 40


class CustomEntity(StrEnum):
    UNIT = "unit"
    PERSON = "person"


class CustomFieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOL = "bool"
    SELECT = "select"


class KvkkCategory(StrEnum):
    IDENTITY = "identity"
    CONTACT = "contact"
    FINANCE = "finance"
    LOCATION_PLATE = "location_plate"
    OTHER = "other"


# Özel nitelikli veri çağrışımı (KVKK m.6): sağlık, din, etnik köken, siyasi, sendika, cinsel
# hayat, ceza mahkûmiyeti, biyometrik/genetik, kan grubu. Karşılaştırma ASCII'ye katlanmış
# küçük harfle yapılır ("Sağlık" = "saglik"); kelime başında eşleşir ("dinlenme" eşleşmez).
SENSITIVE_STEMS: tuple[str, ...] = (
    "saglik", "hastalik", "engelli", "ilac", "tedavi", "din", "mezhep", "inanc",
    "etnik", "irk", "siyasi", "parti uyeligi", "sendika", "cinsel", "ceza", "sabika", "mahkum",
    "biyometrik", "parmak izi", "genetik", "kan grubu", "kiyafet",  # kılık ve kıyafet
)  # fmt: skip
_EXACT_ONLY = frozenset({"din", "irk"})  # kısa kökler yalnız tam kelime olarak
_WORD = re.compile(r"[a-z0-9]+")


def is_sensitive_label(label: str) -> bool:
    text = ascii_fold(label)
    words = _WORD.findall(text)
    joined = " ".join(words)
    for stem in SENSITIVE_STEMS:
        if stem in _EXACT_ONLY:
            if stem in words:
                return True
        elif " " in stem:
            if stem in joined:
                return True
        elif any(word.startswith(stem) for word in words):
            return True
    return False


def field_key(label: str) -> str:
    """field_key("Evcil hayvan") → "evcil_hayvan" (ASCII, alt çizgi, en fazla 40)."""
    words = _WORD.findall(ascii_fold(label))
    return "_".join(words)[:KEY_MAX].strip("_")


class FieldValueError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def coerce_value(
    label: str, field_type: CustomFieldType, options: list[str], raw: object
) -> object:
    """Değeri tipe göre doğrular, saklanacak JSON değerine çevirir; hatalıysa `FieldValueError`."""
    if field_type is CustomFieldType.BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and tr_lower(raw) in {"true", "false", "evet", "hayır"}:
            return tr_lower(raw) in {"true", "evet"}
        raise FieldValueError(f"{label} için Evet ya da Hayır seçin.")
    # JSON sayısı (tam ya da ondalık) metne çevrilip Decimal ile okunur.
    if isinstance(raw, bool) or not isinstance(raw, (str, numbers.Real)):
        raise FieldValueError(f"{label} için geçerli bir değer girin.")
    text = " ".join(str(raw).split())
    if field_type is CustomFieldType.NUMBER:
        try:
            number = Decimal(text.replace(",", "."))
        except InvalidOperation:
            raise FieldValueError(f"{label} sayı olmalı.") from None
        if not number.is_finite() or abs(number) >= Decimal("1e12"):
            raise FieldValueError(f"{label} sayı olmalı.")
        return format(number.normalize(), "f")
    if field_type is CustomFieldType.DATE:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise FieldValueError(f"{label} tarih olmalı (GG.AA.YYYY).") from None
    if field_type is CustomFieldType.SELECT:
        if text not in options:
            raise FieldValueError(f"{label} için listeden seçin.")
        return text
    if len(text) > TEXT_MAX:
        raise FieldValueError(f"{label} en fazla {TEXT_MAX} karakter olabilir.")
    return text
