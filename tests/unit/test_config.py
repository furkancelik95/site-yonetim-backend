import pytest
from pydantic import ValidationError

from site_yonetim.core.config import Environment
from tests.conftest import SettingsFactory

STRONG_SECRET = "x" * 40


def test_virgullu_liste_ayrilir(make_settings: SettingsFactory) -> None:
    settings = make_settings(cors_origins="http://a.test, http://b.test")

    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_yildiz_reddedilir(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        make_settings(cors_origins="*")


@pytest.mark.parametrize("secret", ["__SET_ME__", "kisa-sir"])
def test_uretimde_zayif_jwt_siri_reddedilir(make_settings: SettingsFactory, secret: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make_settings(environment="production", jwt_secret=secret)


def test_uretimde_host_yildiz_reddedilir(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        make_settings(environment="production", jwt_secret=STRONG_SECRET, allowed_hosts="*")


def test_gelistirmede_yer_tutucu_sir_kabul_edilir(make_settings: SettingsFactory) -> None:
    assert make_settings(environment="development").environment is Environment.DEVELOPMENT


def test_jwt_siri_repr_icinde_gorunmez(make_settings: SettingsFactory) -> None:
    settings = make_settings(jwt_secret="cok-gizli-deger")

    assert "cok-gizli-deger" not in repr(settings)
    assert "cok-gizli-deger" not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("environment", "seed", "expected"),
    [
        ("development", True, True),
        ("development", False, False),
        ("test", True, False),
        ("production", True, False),
    ],
)
def test_demo_verisi_yalniz_gelistirmede(
    make_settings: SettingsFactory, environment: str, seed: bool, expected: bool
) -> None:
    settings = make_settings(environment=environment, seed_demo_data=seed, jwt_secret=STRONG_SECRET)

    assert settings.demo_data_allowed is expected


def test_uretimde_guvensiz_cerez_reddedilir(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="REFRESH_COOKIE_SECURE"):
        make_settings(environment="production", refresh_cookie_secure=False)
