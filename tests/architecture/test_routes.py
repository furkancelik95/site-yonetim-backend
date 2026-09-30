"""Uç nokta güvenlik kuralları.

Erişim kontrolü (docs/02 §4 adım 3, Dilim 2) gelene kadar hiçbir gerçek uç nokta
`site_context`'e bağlanmaz: aksi halde siteye erişimi olmayan biri de veriye ulaşırdı.
Kimlik doğrulaması eklendiğinde bu test "site_context kullanan her uç kimlik de ister"
kuralına dönüştürülür.
"""

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from site_yonetim.api.deps import site_context
from site_yonetim.main import create_app
from tests.conftest import SettingsFactory


def _calls(dependant: Dependant) -> set[object]:
    found: set[object] = {dependant.call} if dependant.call else set()
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def test_site_baglamina_bagli_gercek_uc_yok(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())
    offenders = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and site_context in _calls(route.dependant)
    ]

    assert offenders == [], "Erişim kontrolü gelmeden site_context kullanılamaz"
