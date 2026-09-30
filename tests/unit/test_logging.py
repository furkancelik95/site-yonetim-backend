import json
import logging
import sys

import pytest

from site_yonetim.core.logging import JsonFormatter, redact_pii, request_id_var


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("giriş: ayse.yilmaz@example.com", "giriş: [e-posta]"),
        ("tel 0532 123 45 67", "tel [telefon]"),
        ("tel +90 532 123 45 67", "tel [telefon]"),
        ("tel 5321234567", "tel [telefon]"),
        ("tc 12345678901", "tc [tc-kimlik]"),
        ("A-12 dairesi 1234.56 TL", "A-12 dairesi 1234.56 TL"),
        ("GET /api/v1/health 200 3.1ms", "GET /api/v1/health 200 3.1ms"),
        (
            "site_id=0199a3b2-7c4e-7000-8000-000000000000",
            "site_id=0199a3b2-7c4e-7000-8000-000000000000",
        ),
    ],
)
def test_kisisel_veri_maskelenir(raw: str, expected: str) -> None:
    assert redact_pii(raw) == expected


def test_log_satiri_json_ve_baglam_icerir() -> None:
    token = request_id_var.set("istek-123")
    try:
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "kullanıcı %s", ("a@b.co",), None
        )
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)

    assert line["request_id"] == "istek-123"
    assert line["message"] == "kullanıcı [e-posta]"
    assert {"site_id", "user_id", "ts", "level"} <= set(line)


def test_istisna_metni_de_maskelenir() -> None:
    try:
        raise ValueError("bulunamadı: ayse@example.com")
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "hata", None, sys.exc_info())
    line = json.loads(JsonFormatter().format(record))

    assert "ayse@example.com" not in line["exc"]
    assert "[e-posta]" in line["exc"]
