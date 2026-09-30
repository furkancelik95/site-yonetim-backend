"""Türkçe metin işlemleri (docs/04-is-kurallari.md §11, §14, §15).

Python'un `upper/lower/title` işlemleri Türkçeyi bilmez (`"i".upper() == "I"`); burada
Türkçe eşleme önce uygulanır. Arama ve karşılaştırmada iki taraf `tr_lower` ile normalize edilir.
"""

import re
from datetime import date
from decimal import Decimal

from site_yonetim.domain.money import round_money

_TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})


def tr_upper(s: str) -> str:
    """tr_upper("yılmaz işçi") → "YILMAZ İŞÇİ" """
    return s.translate(_TR_UPPER).upper()


def tr_lower(s: str) -> str:
    """tr_lower("İSTANBUL IŞIK") → "istanbul ışık" """
    return s.translate(_TR_LOWER).lower()


def tr_title(s: str) -> str:
    """Her kelimenin baş harfi büyük, gerisi küçük: tr_title("aYşE") → "Ayşe" """
    return " ".join(tr_upper(w[:1]) + tr_lower(w[1:]) for w in s.split(" "))


def normalize_person_name(first_name: str, last_name: str) -> tuple[str, str]:
    """Kayıttaki biçim: ad baş harf büyük (`Ayşe`), soyad tamamı büyük (`YILMAZ`).

    Fazla boşluklar tek boşluğa indirilir.
    """
    first = " ".join(first_name.split())
    last = " ".join(last_name.split())
    return tr_title(first), tr_upper(last)


# --- Adres eki (slug) — §11 ------------------------------------------------

SLUG_MAX_LENGTH = 60
# Türkçe harfler elle eşlenir; `I` ve `İ` slug'da `i` olur (ı değil).
_SLUG_MAP = str.maketrans(
    {
        "ç": "c", "Ç": "c",
        "ğ": "g", "Ğ": "g",
        "ı": "i", "I": "i", "İ": "i", "i": "i",
        "ö": "o", "Ö": "o",
        "ş": "s", "Ş": "s",
        "ü": "u", "Ü": "u",
    }
)  # fmt: skip
_SLUG_SEPARATORS = re.compile(r"[\s\-_.]+")
_SLUG_DISALLOWED = re.compile(r"[^a-z0-9-]")
_SLUG_DASHES = re.compile(r"-{2,}")


def slugify(name: str) -> str:
    """Site adından URL eki: "Aksu Konakları" → "aksu-konaklari".

    Yalnız `a-z`, `0-9` ve `-` kalır; en fazla 60 karakter. Benzersizlik ve en az uzunluk
    kontrolü çağıranın işidir (site kurulumu, docs/10-demo-veri.md §3).
    """
    text = name.strip().translate(_SLUG_MAP).lower()
    text = _SLUG_SEPARATORS.sub("-", text)
    text = _SLUG_DISALLOWED.sub("", text)
    text = _SLUG_DASHES.sub("-", text).strip("-")
    return text[:SLUG_MAX_LENGTH].rstrip("-")


# --- tr-TR biçimleme — §14 --------------------------------------------------
# Yalnız backend'in ürettiği hazır cümlelerde (hareket açıklaması, uyarı) kullanılır.
# API alanlarındaki değerler her zaman makine biçimindedir ("1234.56", "2026-06-15").


def format_money_tr(amount: Decimal) -> str:
    """format_money_tr(Decimal("1234.56")) → "1.234,56 TL" """
    rounded = round_money(amount)
    sign = "-" if rounded < 0 else ""
    integer, _, fraction = f"{abs(rounded):f}".partition(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    return f"{sign}{grouped},{fraction or '00'} TL"


def format_date_tr(value: date) -> str:
    """format_date_tr(date(2026, 6, 15)) → "15.06.2026" """
    return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"


def format_period_tr(year: int, month: int) -> str:
    """format_period_tr(2026, 6) → "06/2026" """
    if not 1 <= month <= 12:
        raise ValueError("Ay 1 ile 12 arasında olmalı.")
    return f"{month:02d}/{year:04d}"


def format_ratio_tr(value: Decimal) -> str:
    """Gereksiz sıfırsız oran: Decimal("0.0450") → "0,045" """
    text = f"{value.normalize():f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")
