import pytest

from site_yonetim.domain.validation import (
    EMERGENCY_SHORT_CODES,
    normalize_email_address,
    normalize_emergency_phone,
    normalize_iban,
    normalize_tax_number,
    normalize_tr_phone,
)


def test_gecerli_iban_bosluksuz_buyuk_harf() -> None:
    assert normalize_iban("tr33 0006 1005 1978 6457 8413 26") == "TR330006100519786457841326"


@pytest.mark.parametrize(
    ("iban", "message"),
    [
        ("TR330006100519786457841327", "kontrol basamağı"),  # son hane değişti
        ("DE89370400440532013000", "'TR' ile"),
        ("TR3300061005", "26 karakter"),
    ],
)
def test_gecersiz_iban(iban: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_iban(iban)


def test_eposta_kucuk_harf_ve_bicim() -> None:
    assert normalize_email_address(" Ayse@Ornek.COM ") == "ayse@ornek.com"
    assert normalize_email_address("a.b+c@mail.ornek.com.tr") == "a.b+c@mail.ornek.com.tr"
    for bad in (
        "ayse",
        "ayse@ornek",
        "a b@ornek.com",
        "x" * 250 + "@a.co",
        "ayse@.ornek.com",
        "ayse@ornek..com",
        "ayse@ornek.com.",
    ):
        with pytest.raises(ValueError, match="e-posta"):
            normalize_email_address(bad)


def test_eposta_deseni_geri_izlemede_patlamaz() -> None:
    """CodeQL py/polynomial-redos: `!@!.` + çok sayıda `!.` eşleşmeyen girdi. Uzunluk sınırını
    aşmadan, sınırsız desende saniyeler sürecek girdi anında reddedilir."""
    from site_yonetim.domain import validation

    hostile = "!@!." + "!." * 5000 + "@"
    assert validation._EMAIL.match(hostile) is None


def test_vkn_10_hane() -> None:
    assert normalize_tax_number(" 1234567890 ") == "1234567890"
    for bad in ("123456789", "12345678901", "12345abcde"):
        with pytest.raises(ValueError, match="10 haneli"):
            normalize_tax_number(bad)


# --- Sabit/cep TR telefonu ve acil kart numarası (test raporu) ------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0212 123 45 67", "+902121234567"),  # sabit, İstanbul
        ("(0312) 555 00 00", "+903125550000"),  # sabit, Ankara
        ("0532 123 45 67", "+905321234567"),  # cep
        ("+90 532 123 45 67", "+905321234567"),
        ("905321234567", "+905321234567"),
        ("2121234567", "+902121234567"),
        ("0850 222 33 44", "+908502223344"),
    ],
)
def test_tr_telefonu_sabit_ve_cep_e164(raw: str, expected: str) -> None:
    assert normalize_tr_phone(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "123", "0212 123 45", "00212 123 45 67", "0012121234567", "+49 151 23456789"]
)
def test_tr_telefonu_gecersiz(raw: str) -> None:
    with pytest.raises(ValueError, match="10 hane"):
        normalize_tr_phone(raw)


def test_acil_kisa_kodlar_beyaz_liste() -> None:
    assert {"112", "110", "155", "156", "177", "122"} <= EMERGENCY_SHORT_CODES
    for code in EMERGENCY_SHORT_CODES:
        assert normalize_emergency_phone(code) == code
    assert normalize_emergency_phone("0212 123 45 67") == "+902121234567"
    with pytest.raises(ValueError, match="Kısa numara"):
        normalize_emergency_phone("999")
    with pytest.raises(ValueError, match="Acil kod"):
        normalize_emergency_phone("12")
