import uuid
from decimal import Decimal

from site_yonetim.models import Unit


def test_arsa_payi_orani() -> None:
    unit = Unit(number="12", land_share_numerator=45, land_share_denominator=1000)

    assert unit.land_share == Decimal("0.045")


def test_arsa_payi_eksikse_yok() -> None:
    assert Unit(number="12").land_share is None
    assert Unit(number="12", land_share_numerator=45, land_share_denominator=0).land_share is None


def test_gorunen_ad() -> None:
    unit = Unit(number="12")

    assert unit.display_name("A") == "A-12"
    assert unit.display_name("") == "12"
    assert unit.display_name(None) == "12"


def test_site_ad_anahtari_turkce_kurallarla() -> None:
    from site_yonetim.models import Site

    assert Site(name="AKSU  KONAKLARI").name_key == Site(name="Aksu Konakları").name_key
    assert Site(name="IŞIK Sitesi").name_key == "ışık sitesi"
    assert Site(name="İSTANBUL").name_key == "istanbul"


def test_kisi_iletisim_bilgisi_izin_yoksa_gizlenir() -> None:
    from site_yonetim.api.v1.structure import PersonOut
    from site_yonetim.models import Person

    person = Person(
        id=uuid.uuid4(),
        first_name="Ayşe",
        last_name="YILMAZ",
        phone="+905321234567",
        email="a@b.co",
    )

    hidden = PersonOut.of(person, contact=False)
    shown = PersonOut.of(person, contact=True)

    assert (hidden.phone, hidden.email) == (None, None)
    assert shown.phone == "+905321234567"
    assert person.search_name == "ayşe yılmaz"
