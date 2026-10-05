import pytest

from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_iban,
    normalize_tax_number,
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
