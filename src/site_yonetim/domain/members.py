"""Site kullanıcıları ve sakin kayıt başvurusu — saf kurallar (servis istekleri 12, 13).

Roller sabittir (docs/05 §3); burada yalnız ekranın kullandığı anahtar ve açıklamalar var.
Kayıt başvurusu alanları kurumsal iletişim formu standardıyla doğrulanır; ekran aynı kuralları
uygular, sunucu **yine** doğrular.
"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from site_yonetim.domain.access import SiteRole
from site_yonetim.domain.text import normalize_person_name
from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_name_part,
    normalize_tr_mobile,
)

# --- Roller -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleInfo:
    key: str
    role: SiteRole
    description: str


STAFF_ROLES: tuple[RoleInfo, ...] = (
    RoleInfo("manager", SiteRole.MANAGER, "Sitenin bütün işlemleri; kullanıcı ve modül yönetimi"),
    RoleInfo("board", SiteRole.BOARD_MEMBER, "Finans, gider ve talepleri okur; değişiklik yapmaz"),
    RoleInfo("auditor", SiteRole.AUDITOR, "Finansı ve denetim kaydını okur; kişisel veri görmez"),
    RoleInfo("accounting", SiteRole.ACCOUNTING, "Tahakkuk, tahsilat, gider, kasa ve raporlar"),
    RoleInfo("security", SiteRole.SECURITY, "Ziyaretçi, kargo ve olay kayıtları; borç görmez"),
    RoleInfo("technical", SiteRole.TECHNICIAN, "Kendisine atanan talepleri yürütür"),
)
BY_KEY = {r.key: r for r in STAFF_ROLES}
BY_ROLE = {r.role.value: r for r in STAFF_ROLES}


# --- Doğrulama ----------------------------------------------------------------------


class Relation(StrEnum):
    OWNER = "owner"  # malik
    TENANT = "tenant"  # kiracı


FULL_NAME_MIN, FULL_NAME_MAX = 3, 80
FORM_ERROR = "Formda düzeltilmesi gereken alanlar var."


class FormFieldError(Exception):
    """Birden çok alan hatası birlikte — 422, `fields`."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__(FORM_ERROR)
        self.fields = fields


def _letters_only(text: str) -> bool:
    return all(c == " " or unicodedata.category(c).startswith("L") for c in text)


def check_name(value: str | None, label: str) -> str:
    """Ad/soyad: 2–40, harf ve boşluk (rakam ve özel karakter yok). Hatalıysa `ValueError`."""
    text = normalize_name_part(value or "", label)
    if not _letters_only(text):
        raise ValueError(f"{label} yalnız harf içerebilir.")
    return text


def check_full_name(value: str | None) -> str:
    """Personel adı soyadı: 3–80, harf, boşluk ve nokta."""
    text = " ".join((value or "").split())
    if not FULL_NAME_MIN <= len(text) <= FULL_NAME_MAX or not _letters_only(text.replace(".", "")):
        raise ValueError(f"Ad soyad {FULL_NAME_MIN}–{FULL_NAME_MAX} karakter, yalnız harf olmalı.")
    return text


@dataclass(frozen=True, slots=True)
class Applicant:
    first_name: str  # `Ayşe`
    last_name: str  # `YILMAZ`
    phone: str
    email: str | None
    unit_text: str
    relation: Relation


def check_applicant(
    *,
    first_name: str | None,
    last_name: str | None,
    phone: str | None,
    email: str | None,
    unit_text: str | None,
    relation: str | None,
    kvkk_ack: bool,
) -> Applicant:
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}
    raw_email = (email or "").strip()
    checks: tuple[tuple[str, Callable[[], str]], ...] = (
        ("first_name", lambda: check_name(first_name, "Ad")),
        ("last_name", lambda: check_name(last_name, "Soyad")),
        ("phone", lambda: normalize_tr_mobile(phone or "")),
        ("email", lambda: normalize_email_address(raw_email) if raw_email else ""),
    )
    for name, check in checks:
        try:
            clean[name] = check()
        except ValueError as exc:
            errors[name] = str(exc)
    unit = " ".join((unit_text or "").split())
    if not 1 <= len(unit) <= 40:
        errors["unit_text"] = "Blok ve daire numaranızı yazın."
    if relation not in {r.value for r in Relation}:
        errors["relation"] = "Malik ya da kiracı olduğunuzu seçin."
    if not kvkk_ack:
        errors["kvkk_ack"] = "Bilgilendirme metnini okuduğunuzu onaylayın."
    if errors:
        raise FormFieldError(errors)
    first, last = normalize_person_name(clean["first_name"], clean["last_name"])
    return Applicant(
        first_name=first,
        last_name=last,
        phone=clean["phone"],
        email=clean["email"] or None,
        unit_text=unit,
        relation=Relation(relation or ""),
    )
