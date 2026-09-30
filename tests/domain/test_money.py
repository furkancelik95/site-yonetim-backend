"""Altın testler — docs/07-test-senaryolari.md §1 (dağıtım) ve 1.9 (yuvarlama)."""

import random
from decimal import Decimal

import pytest

from site_yonetim.domain.money import distribute, is_positive, round_money
from tests.domain.sample_site import TEST_SITE_UNITS

D = Decimal


def test_test_sitesi_24_aktif_daire() -> None:
    assert len(TEST_SITE_UNITS) == 24
    assert sum(1 for unit in TEST_SITE_UNITS if unit.block == "A") == 12


def test_1_1_esit_dagitimda_toplam_korunur() -> None:
    result = distribute(D("100"), [D(1), D(1), D(1)])

    assert result == [D("33.34"), D("33.33"), D("33.33")]
    assert sum(result) == D("100")


def test_1_2_agirlikli_dagitimda_toplam_korunur_ve_her_pay_pozitif() -> None:
    weights = [unit.type_weight for unit in TEST_SITE_UNITS]

    result = distribute(D("20000"), weights)

    assert sum(result) == D("20000")
    assert all(share > 0 for share in result)


def test_1_3_arsa_payina_gore_toplam_korunur() -> None:
    weights = [unit.land_share for unit in TEST_SITE_UNITS]

    result = distribute(D("180000"), weights)

    assert sum(result) == D("180000")


@pytest.mark.parametrize(
    ("total", "count"),
    [(D("0.01"), 3), (D("0.02"), 3), (D("1000.05"), 7), (D("99999.99"), 13)],
)
def test_1_4_bolunmeyen_kuruslar_kaybolmaz(total: Decimal, count: int) -> None:
    assert sum(distribute(total, [D(1)] * count)) == total


def test_1_5_tek_daireye_tamami_yazilir() -> None:
    assert distribute(D("1234.56"), [D(1)]) == [D("1234.56")]


def test_1_6_sifir_agirlik_toplami_reddedilir() -> None:
    with pytest.raises(ValueError, match="sıfır veya negatif"):
        distribute(D("100"), [D(0), D(0)])


def test_1_7_bos_liste_bos_sonuc() -> None:
    assert distribute(D("100"), []) == []


def test_1_8_rastgele_20000_dagitimda_toplam_korunur() -> None:
    rng = random.Random(20260930)  # noqa: S311 — güvenlik değil, tekrarlanabilir test verisi
    for _ in range(20_000):
        total = D(rng.randint(-10_000_000, 10_000_000)) / 100
        weights = [D(rng.randint(0, 5000)) / 1000 for _ in range(rng.randint(1, 40))]
        if sum(weights) == 0:
            weights[0] = D(1)

        result = distribute(total, weights)

        assert sum(result) == total, (total, weights)
        assert len(result) == len(weights)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D("2.345"), D("2.35")),
        (D("-2.345"), D("-2.35")),
        (D("2.344"), D("2.34")),
        (D("0.125"), D("0.13")),  # ROUND_HALF_EVEN burada 0.12 verirdi
        (D("2.5"), D("2.50")),
    ],
)
def test_1_9_yuvarlama_yarim_yukari(value: Decimal, expected: Decimal) -> None:
    assert round_money(value) == expected


# --- Ek güvenceler ----------------------------------------------------------


def test_kuruslar_kesilen_kismi_buyuk_olana_gider() -> None:
    # ham: 33.333…, 66.666… → kesilen: 0.00333, 0.00666 → eksik kuruş ikinciye
    assert distribute(D("100"), [D(1), D(2)]) == [D("33.33"), D("66.67")]


def test_kesilen_kisim_esitse_agirligi_buyuk_olan_once() -> None:
    # 0.03, ağırlık [1, 1, 1, 3]: ham 0.005 ×3 ve 0.015 → kesilen kısım hepsinde 0.005.
    # 2 eksik kuruş: önce ağırlığı büyük olan (4.), sonra liste sırasıyla ilk.
    assert distribute(D("0.03"), [D(1), D(1), D(1), D(3)]) == [
        D("0.01"),
        D("0.00"),
        D("0.00"),
        D("0.02"),
    ]


def test_her_sey_esitse_liste_sirasi_korunur() -> None:
    assert distribute(D("0.02"), [D(1), D(1), D(1)]) == [D("0.01"), D("0.01"), D("0.00")]


def test_negatif_tutar_pozitifin_aynasi() -> None:
    weights = [D("1.3"), D("1.0"), D("1.3"), D("0.7")]
    positive = distribute(D("1000.05"), weights)

    assert distribute(D("-1000.05"), weights) == [-share for share in positive]


def test_sifir_tutar_sifir_paylar() -> None:
    assert distribute(D("0"), [D(1), D(2)]) == [D("0.00"), D("0.00")]


def test_negatif_agirlik_reddedilir() -> None:
    with pytest.raises(ValueError, match="negatif"):
        distribute(D("100"), [D(2), D(-1)])


def test_kurustan_hassas_tutar_reddedilir() -> None:
    with pytest.raises(ValueError, match="kuruşa"):
        distribute(D("100.005"), [D(1), D(1)])


@pytest.mark.parametrize("bad", [100.0, 100])
def test_float_ve_int_para_olarak_kabul_edilmez(bad: object) -> None:
    with pytest.raises(TypeError):
        distribute(bad, [D(1)])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        round_money(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(D("0.01"), True), (D("0.006"), True), (D("0.005"), False), (D("0"), False), (D("-1"), False)],
)
def test_sifirdan_buyuk_toleransli(value: Decimal, expected: bool) -> None:
    assert is_positive(value) is expected
