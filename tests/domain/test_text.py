"""Türkçe metin, slug ve tr-TR biçimleme — docs/04-is-kurallari.md §11, §14, §15."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.text import (
    SLUG_MAX_LENGTH,
    format_date_tr,
    format_money_tr,
    format_period_tr,
    format_ratio_tr,
    normalize_person_name,
    slugify,
    tr_lower,
    tr_title,
    tr_upper,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yılmaz işçi", "YILMAZ İŞÇİ"), ("istanbul", "İSTANBUL"), ("ığdır", "IĞDIR")],
)
def test_tr_upper(raw: str, expected: str) -> None:
    assert tr_upper(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("İSTANBUL", "istanbul"), ("IŞIK", "ışık"), ("ÇAĞRI", "çağrı")],
)
def test_tr_lower(raw: str, expected: str) -> None:
    result = tr_lower(raw)

    assert result == expected
    assert len(result) == len(raw)  # "İ".lower() gibi 2 karakterlik sonuç yok


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("aYşE", "Ayşe"), ("işçi", "İşçi"), ("ILGIN", "Ilgın"), ("ali veli", "Ali Veli")],
)
def test_tr_title(raw: str, expected: str) -> None:
    assert tr_title(raw) == expected


def test_isim_bicimi_duzeltilir_5_10() -> None:
    # docs/07 §5.10: ad `aYşE`, soyad `yılmaz` → `Ayşe`, `YILMAZ`
    assert normalize_person_name("aYşE", "yılmaz") == ("Ayşe", "YILMAZ")


def test_isimde_fazla_bosluk_temizlenir() -> None:
    assert normalize_person_name("  ayşe   nur ", " yılmaz ") == ("Ayşe Nur", "YILMAZ")


def test_aramada_turkce_normalizasyon() -> None:
    assert tr_lower("İstanbul") == tr_lower("istanbul")
    assert tr_lower("IŞIK") == tr_lower("ışık")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Aksu Konakları", "aksu-konaklari"),  # docs/10 demo siteleri
        ("Yıldız Sitesi", "yildiz-sitesi"),
        ("Mimoza Apartmanı", "mimoza-apartmani"),
        ("  İSTANBUL Işık Plaza  ", "istanbul-isik-plaza"),
        ("Çağlayan  Ğ-Ş_Ü.Ö", "caglayan-g-s-u-o"),
        ("A&B Rezidans (Blok 2)", "ab-rezidans-blok-2"),
        ("--Deniz--Evleri--", "deniz-evleri"),
        ("Café Résidence", "caf-rsidence"),  # §11: Türkçe dışı harf atılır
        ("<script>", "script"),
        ("../../etc", "etc"),
        ("", ""),
    ],
)
def test_slug(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_slug_en_fazla_60_karakter_ve_tire_ile_bitmez() -> None:
    slug = slugify("a" * 59 + " bcd")

    assert len(slug) <= SLUG_MAX_LENGTH
    assert not slug.endswith("-")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("1234.56"), "1.234,56 TL"),
        (Decimal("0"), "0,00 TL"),
        (Decimal("-500"), "-500,00 TL"),
        (Decimal("1234567.8"), "1.234.567,80 TL"),
        (Decimal("999.995"), "1.000,00 TL"),
    ],
)
def test_para_tr_bicimi(amount: Decimal, expected: str) -> None:
    assert format_money_tr(amount) == expected


def test_tarih_ve_donem_tr_bicimi() -> None:
    assert format_date_tr(date(2026, 6, 15)) == "15.06.2026"
    assert format_period_tr(2026, 6) == "06/2026"


def test_gecersiz_ay_reddedilir() -> None:
    with pytest.raises(ValueError, match="Ay"):
        format_period_tr(2026, 13)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.0450"), "0,045"),
        (Decimal("1.3500"), "1,35"),
        (Decimal("2"), "2"),
        (Decimal("100"), "100"),
    ],
)
def test_oran_gereksiz_sifirsiz(value: Decimal, expected: str) -> None:
    assert format_ratio_tr(value) == expected
