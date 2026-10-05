"""Uç nokta güvenlik kuralları — varsayılan kapalı.

1. Her uç kimlik (`current_user`) ister; istisnalar aşağıdaki beyaz listededir.
2. `/sites/{slug}/…` altındaki her uç `site_context`'ten geçer (erişim 404, kapsam, modül).
3. `site_context` kullanan her uç kimlik de ister.
4. `/platform/…` altındaki her uç `platform_admin`'den geçer (değilse 404).

Not: FastAPI alt router'ları `app.routes` içinde düzleştirmez; uçlar OpenAPI üretiminin de
kullandığı `iter_route_contexts` ile dolaşılır. Testin boşa geçmediği ayrıca doğrulanır.
"""

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from site_yonetim.api.deps import current_user, password_changed, platform_admin, site_context
from site_yonetim.main import create_app
from tests.conftest import SettingsFactory

# Kimliksiz erişilebilen uçlar. Buraya ekleme bilinçli bir güvenlik kararıdır.
PUBLIC_ROUTES = {
    "/api/v1/health",
    "/api/v1/health/ready",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",  # çerezle doğrular
    "/api/v1/auth/logout",  # her zaman 204; varsa oturumu iptal eder
    # Sakinin kendini kaydetmesi (servis isteği 13): IP başına istek sınırı, tahmin edilemez kod,
    # yalnız site adı döner, başvuru yönetici onayı bekler.
    "/api/v1/public/registration/{code}",
}
# Geçici parolalı oturumun erişebildiği uçlar (docs/05 §8.1). Gerisi `password_changed` ister.
PASSWORD_PENDING_ROUTES = {"/api/v1/me", "/api/v1/auth/change-password"}


def _calls(dependant: Dependant) -> set[object]:
    found: set[object] = {dependant.call} if dependant.call else set()
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def _api_routes(app: FastAPI) -> list[tuple[str, set[object]]]:
    routes = []
    for context in iter_route_contexts(app.routes):
        if isinstance(context.route, APIRoute):
            routes.append((str(context.path), _calls(context.route.dependant)))
    return routes


@pytest.fixture
def routes(make_settings: SettingsFactory) -> list[tuple[str, set[object]]]:
    return _api_routes(create_app(make_settings()))


def test_uclar_gercekten_dolasiliyor(routes: list[tuple[str, set[object]]]) -> None:
    paths = {path for path, _ in routes}

    assert {"/api/v1/me", "/api/v1/sites/{slug}", "/api/v1/auth/login"} <= paths


def test_her_uc_kimlik_ister(routes: list[tuple[str, set[object]]]) -> None:
    offenders = [
        path for path, calls in routes if path not in PUBLIC_ROUTES and current_user not in calls
    ]

    assert offenders == [], (
        "Kimliksiz uç: current_user ekleyin ya da bilinçli olarak beyaz listeye alın"
    )


def test_gecici_parolada_yalniz_me_ve_parola_degisikligi(
    routes: list[tuple[str, set[object]]],
) -> None:
    offenders = [
        path
        for path, calls in routes
        if path not in PUBLIC_ROUTES | PASSWORD_PENDING_ROUTES and password_changed not in calls
    ]
    paths = {path for path, _ in routes}

    assert paths >= PASSWORD_PENDING_ROUTES
    assert offenders == [], "Bu uç geçici parolalı oturuma açık: CurrentUserDep kullanın"


def test_site_uclari_site_baglamindan_gecer(routes: list[tuple[str, set[object]]]) -> None:
    offenders = [
        path for path, calls in routes if "/sites/{slug}" in path and site_context not in calls
    ]

    assert offenders == []


def test_platform_uclari_platform_yoneticisi_ister(routes: list[tuple[str, set[object]]]) -> None:
    platform_paths = [path for path, _ in routes if path.startswith("/api/v1/platform")]
    offenders = [
        path
        for path, calls in routes
        if path.startswith("/api/v1/platform") and platform_admin not in calls
    ]

    assert platform_paths, "platform uçları dolaşılamadı"
    assert offenders == []


def test_site_baglami_kimlik_ister(routes: list[tuple[str, set[object]]]) -> None:
    offenders = [
        path for path, calls in routes if site_context in calls and current_user not in calls
    ]

    assert offenders == []


def test_kural_ihlali_yakalanir(make_settings: SettingsFactory) -> None:
    from fastapi import APIRouter

    app = create_app(make_settings())
    router = APIRouter(prefix="/api/v1")

    @router.get("/acik-uc")
    async def open_endpoint() -> None:
        return None

    app.include_router(router)
    offenders = [
        p for p, calls in _api_routes(app) if p not in PUBLIC_ROUTES and current_user not in calls
    ]

    assert offenders == ["/api/v1/acik-uc"]
