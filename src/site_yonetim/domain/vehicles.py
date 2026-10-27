"""Araçlar — saf kurallar (servis isteği 22, Yazılım Dokümanı YAP-05).

Plaka kişisel veridir (KVKK kategori `location_plate`). Saklanırken büyük harf ve boşluksuz,
gösterilirken `34 ABC 123` biçimindedir.
"""

import re
from enum import StrEnum

PLATE_RE = re.compile(r"^(0[1-9]|[1-7]\d|8[01])[A-Z]{1,3}\d{2,5}$")
_PARTS = re.compile(r"^(\d{2})([A-Z]+)(\d+)$")
PLATE_LIMIT_MIN, PLATE_LIMIT_MAX, PLATE_LIMIT_DEFAULT = 1, 10, 2
PLATE_FORMAT_MESSAGE = "Plakayı 34 ABC 123 biçiminde yazın."

# Türkçe klavyeden gelen harfler plakada ASCII karşılığına çevrilir (İ → I, ı → I).
_TR_PLATE = str.maketrans(
    {"i": "I", "ı": "I", "İ": "I", "ş": "S", "Ş": "S", "ğ": "G", "Ğ": "G"}
    | {"ü": "U", "Ü": "U", "ö": "O", "Ö": "O", "ç": "C", "Ç": "C"}
)


class VehicleKind(StrEnum):
    PERMANENT = "permanent"  # kalıcı — bölüm sınırına sayılır
    GUEST = "guest"  # misafir — sınıra sayılmaz, geçerlilik tarihi zorunlu


class VehicleStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"  # sakin kaydı, yönetim onayı bekliyor
    INACTIVE = "inactive"  # kaldırıldı / pasif — kapı ekranında görünmez, geçmişte kalır


def normalize_plate(value: str) -> str:
    """normalize_plate(" 34 abc 123 ") → "34ABC123" (yalnız harf ve rakam, büyük harf)."""
    folded = value.translate(_TR_PLATE).upper()
    return "".join(ch for ch in folded if ch.isascii() and ch.isalnum())


def is_valid_plate(plate: str) -> bool:
    return bool(PLATE_RE.match(plate))


def display_plate(plate: str) -> str:
    """display_plate("34ABC123") → "34 ABC 123"."""
    match = _PARTS.match(plate)
    return " ".join(match.groups()) if match else plate
