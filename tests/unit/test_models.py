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
