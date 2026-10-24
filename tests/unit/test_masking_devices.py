"""Maskeleme ve cihaz adı — saf kurallar (domain/masking.py, domain/devices.py)."""

import pytest

from site_yonetim.domain.devices import UNKNOWN, describe_device
from site_yonetim.domain.masking import mask_email, mask_iban, mask_phone, mask_tail, mask_value


def test_telefon_son_dort_hane() -> None:
    assert mask_phone("+905321234567") == "+90******4567"
    assert mask_phone("05321234567") == "*******4567"


def test_eposta_ilk_harf_ve_alan_adi() -> None:
    assert mask_email("ayse.yilmaz@ornek.com") == "a***@ornek.com"
    assert mask_email("bozuk") == "*ozuk"


def test_iban_ulke_ve_son_dort() -> None:
    assert mask_iban("TR330006100519786457841326") == "TR" + "*" * 20 + "1326"
    assert mask_iban("TR12") == "****"


def test_kisa_deger_tamamen_gizlenir() -> None:
    assert mask_tail("123") == "***"


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("phone", "+905321234567", "+90******4567"),
        ("email", "x@y.com", "x***@y.com"),
        ("tax_number", "1234567890", "******7890"),
        ("first_name", "Ayşe", "Ayşe"),  # hassas değil
        ("phone", None, None),
        ("phone", "", ""),
        ("iban", 12, 12),  # metin değilse dokunulmaz
    ],
)
def test_sutuna_gore_maskeleme(column: str, value: object, expected: object) -> None:
    assert mask_value(column, value) == expected


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 "
            "Safari/537.36 Edg/128.0",
            "Edge · Windows",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Chrome · Windows",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Safari/604.1",
            "Safari · iPhone",
        ),
        (
            "Mozilla/5.0 (Linux; Android 15; SM-S928B) Chrome/128.0 Mobile Safari/537.36",
            "Chrome · Android",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6; rv:131.0) Gecko/20100101 Firefox/131.0",
            "Firefox · macOS",
        ),
        ("python-httpx/0.28", UNKNOWN),
        (None, UNKNOWN),
        ("", UNKNOWN),
    ],
)
def test_cihaz_adi(agent: str | None, expected: str) -> None:
    assert describe_device(agent) == expected
