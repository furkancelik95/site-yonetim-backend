"""Kayıt başvurusu ve personel adı doğrulaması — saf kurallar (servis istekleri 12, 13)."""

import pytest

from site_yonetim.domain.members import (
    BY_KEY,
    STAFF_ROLES,
    FormFieldError,
    Relation,
    check_applicant,
    check_full_name,
)


def applicant(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "first_name": "  ayşe  nur ",
        "last_name": "yılmaz",
        "phone": "+90 (532) 111-22-33",
        "email": " Ayse@Ornek.Local ",
        "unit_text": " A  blok 4 ",
        "relation": "owner",
        "kvkk_ack": True,
    }
    data.update(overrides)
    return data


def test_gecerli_basvuru_bicimlenir() -> None:
    result = check_applicant(**applicant())  # type: ignore[arg-type]
    assert (result.first_name, result.last_name) == ("Ayşe Nur", "YILMAZ")
    assert (result.phone, result.email, result.unit_text) == (
        "+905321112233", "ayse@ornek.local", "A blok 4"
    )  # fmt: skip
    assert result.relation is Relation.OWNER


def test_eposta_istege_bagli() -> None:
    assert check_applicant(**applicant(email="  ")).email is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"first_name": "A"}, "first_name"),
        ({"first_name": "Ali2"}, "first_name"),
        ({"last_name": "O'Neil"}, "last_name"),
        ({"phone": "0212 111 22 33"}, "phone"),
        ({"email": "a@b"}, "email"),
        ({"unit_text": ""}, "unit_text"),
        ({"relation": "agent"}, "relation"),
        ({"kvkk_ack": False}, "kvkk_ack"),
    ],
)
def test_alan_hatalari(overrides: dict[str, object], field: str) -> None:
    with pytest.raises(FormFieldError) as exc:
        check_applicant(**applicant(**overrides))  # type: ignore[arg-type]
    assert set(exc.value.fields) == {field}


def test_personel_adi() -> None:
    assert check_full_name("  Selin   Arı ") == "Selin Arı"
    assert check_full_name("Dr. Ali Can") == "Dr. Ali Can"
    for bad in ("Al", "Ali 2", "x" * 81):
        with pytest.raises(ValueError, match="Ad soyad"):
            check_full_name(bad)


def test_roller_sabit() -> None:
    assert [r.key for r in STAFF_ROLES] == list(BY_KEY)
    assert "resident" not in BY_KEY
