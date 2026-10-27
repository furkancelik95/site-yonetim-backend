"""İletişim saf kuralları — sessiz saat, SMS parça sayısı, şablon değişkenleri, kroki, dönem."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from site_yonetim.domain.communication import (
    SYSTEM_BY_CODE,
    Variant,
    check_period,
    check_variants,
    in_quiet,
    shift_out_of_quiet,
    sms_count,
    valid_map,
    valid_phone,
)

TZ = ZoneInfo("Europe/Istanbul")
Q = (time(22), time(8))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (time(21, 59), False),
        (time(22), True),
        (time(3), True),
        (time(7, 59), True),
        (time(8), False),
    ],
)
def test_sessiz_saat_gece_yarisini_gecer(moment: time, expected: bool) -> None:
    assert in_quiet(moment, *Q) is expected
    assert in_quiet(moment, time(9), time(9)) is False


def test_sessiz_saatte_bitise_kayar() -> None:
    late = datetime(2026, 6, 20, 23, 30, tzinfo=TZ)
    assert shift_out_of_quiet(late, *Q) == (datetime(2026, 6, 21, 8, 0, tzinfo=TZ), True)
    early = datetime(2026, 6, 21, 6, 0, tzinfo=TZ)
    assert shift_out_of_quiet(early, *Q) == (datetime(2026, 6, 21, 8, 0, tzinfo=TZ), True)
    noon = datetime(2026, 6, 21, 12, 0, tzinfo=TZ)
    assert shift_out_of_quiet(noon, *Q) == (noon, False)


def test_sms_parca_sayisi() -> None:
    assert sms_count("a" * 160).segments == 1
    assert sms_count("a" * 161).segments == 2  # 153'lük parçalar
    assert (sms_count("ş" * 70).encoding, sms_count("ş" * 70).segments) == ("Unicode", 1)
    assert sms_count("ş" * 71).segments == 2  # 67'lik parçalar
    assert sms_count("").segments == 0


def test_sablon_degiskenleri() -> None:
    dues = SYSTEM_BY_CODE["dues_reminder"]
    assert check_variants(dues, [Variant("tr", None, "{{tutar}} {{vade}}")]) == {}
    assert check_variants(dues, [Variant("en", None, "{{tutar}} {{vade}}")]) == {
        "variants": "Türkçe metin zorunlu."
    }
    ann = SYSTEM_BY_CODE["ann_water_outage"]
    assert check_variants(ann, [Variant("tr", None, "{{tarih}} su yok")]) == {
        "variants.tr": "Duyuru şablonunda başlık zorunlu."
    }


def test_kroki_ve_telefon() -> None:
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    assert valid_map(png)
    assert not valid_map("data:image/svg+xml;base64,PHN2Zz4=")
    assert not valid_map("data:image/png;base64,/9j/4AAQSkZJRgABAQ")  # JPEG içerik PNG diye
    assert valid_phone("112")
    assert valid_phone("+905551112233")
    assert not valid_phone("12")


def test_seffaflik_donemi() -> None:
    today = date(2026, 6, 20)
    assert check_period(2026, 6, today) is None
    assert check_period(2026, 7, today) == "Gelecek bir dönem için rapor hazırlanamaz."
    assert check_period(2026, 13, today) == "Geçerli bir dönem seçin."
