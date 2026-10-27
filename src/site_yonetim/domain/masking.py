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


def mask_all(value: str) -> str:
    """Tamamı gizli: değişikliğin olduğu görünür, değer görünmez (ör. izin türü — raporlu izin
    sağlık verisidir)."""
    return "***"


# Sütun adı → maskeleyici. Denetlenen her tabloda aynı adlı sütun aynı biçimde maskelenir.
SENSITIVE_COLUMNS: dict[str, Callable[[str], str]] = {
    "phone": mask_phone,
    "email": mask_email,
    "iban": mask_iban,
    "tax_number": mask_tail,
    "leave_type": mask_all,  # IK-03: raporlu izin özel nitelikli veri sayılabilir
    "leave_note": mask_all,
    "qr_code": mask_all,  # giriş-çıkış doğrulama kodu
    "to_phone": mask_phone,  # OPS-01: anahtarı teslim alanın telefonu
    "authorized": mask_all,  # OPS-01: emanet anahtar yetkili kişileri (ad + telefon listesi)
}


def mask_value(column: str, value: object) -> object:
    masker = SENSITIVE_COLUMNS.get(column)
    if masker is mask_all and isinstance(value, (list, dict)) and value:
        return mask_all("")  # JSON değer (ör. kişi listesi) bütünüyle gizlenir
    if masker is None or not isinstance(value, str) or not value:
        return value
    return masker(value)


def mask_phone_display(value: str) -> str:
    """Ekranda maskeli telefon: "+905321112233" → "+90 532 *** ** 33" (YAP-03 S2)."""
    if value.startswith("+90") and len(value) == 13:
        return f"+90 {value[3:6]} *** ** {value[-2:]}"
    return mask_phone(value)
