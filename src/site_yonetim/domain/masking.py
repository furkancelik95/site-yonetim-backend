"""Kişisel veri maskeleme — saf (Yazılım Dokümanı M-24, PLT-06; docs/09 §6).

Denetim kaydı kişisel veri ifşası için kullanılamaz: telefon, e-posta, IBAN ve vergi no kayda
maskeli yazılır (işlemi yapanın IP'si güvenlik incelemesi için açık kalır); maskeyi açan bir
yol yoktur. Değişikliğin **olduğu** görünür, değerin kendisi görünmez (son haneler hangi
kaydın değiştiğini ayırt etmeye yeter).
"""

from collections.abc import Callable

_STAR = "*"


def mask_tail(value: str, keep: int = 4) -> str:
    """Son `keep` karakter dışında yıldız: mask_tail("1234567890") → "******7890"."""
    if len(value) <= keep:
        return _STAR * len(value)
    return _STAR * (len(value) - keep) + value[-keep:]


def mask_phone(value: str) -> str:
    """mask_phone("+905321234567") → "+90******4567" (ülke kodu ve son 4 hane)."""
    if value.startswith("+") and len(value) > 7:
        return value[:3] + mask_tail(value[3:])
    return mask_tail(value)


def mask_email(value: str) -> str:
    """mask_email("ayse.yilmaz@ornek.com") → "a***@ornek.com"."""
    local, at, domain = value.partition("@")
    if not at:
        return mask_tail(value)
    return f"{local[:1]}***@{domain}"


def mask_iban(value: str) -> str:
    """mask_iban("TR330006100519786457841326") → "TR********************1326"."""
    if len(value) <= 6:
        return _STAR * len(value)
    return value[:2] + _STAR * (len(value) - 6) + value[-4:]


# Sütun adı → maskeleyici. Denetlenen her tabloda aynı adlı sütun aynı biçimde maskelenir.
SENSITIVE_COLUMNS: dict[str, Callable[[str], str]] = {
    "phone": mask_phone,
    "email": mask_email,
    "iban": mask_iban,
    "tax_number": mask_tail,
}


def mask_value(column: str, value: object) -> object:
    masker = SENSITIVE_COLUMNS.get(column)
    if masker is None or not isinstance(value, str) or not value:
        return value
    return masker(value)
