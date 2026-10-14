"""Parola hash'i ve erişim jetonu güvenliği."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from site_yonetim.core.security import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from tests.conftest import SettingsFactory

NOW = datetime.now(UTC)


def test_parola_argon2id_ile_saklanir_ve_dogrulanir() -> None:
    hashed = hash_password("Demo1234!")

    assert hashed.startswith("$argon2id$")
    assert "Demo1234!" not in hashed
    assert verify_password(hashed, "Demo1234!")
    assert not verify_password(hashed, "demo1234!")


def test_hash_yoksa_ya_da_bozuksa_dogrulama_basarisiz() -> None:
    assert not verify_password(None, "herhangi")
    assert not verify_password("bozuk-hash", "herhangi")


def test_erisim_jetonu_gidis_donus(make_settings: SettingsFactory) -> None:
    settings = make_settings()
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    # "şimdi" test anında alınır: modül yüklenirken alınan NOW, uzun bir takımda 15 dakikalık
    # jeton ömrünü aşabilir (decode gerçek saati kullanır).
    now = datetime.now(UTC)
    token = create_access_token(settings, user_id=user_id, session_id=session_id, now=now)
    claims = decode_access_token(settings, token)

    assert (claims.user_id, claims.session_id) == (user_id, session_id)


def test_suresi_dolmus_jeton_reddedilir(make_settings: SettingsFactory) -> None:
    settings = make_settings()
    old = NOW - timedelta(minutes=settings.jwt_access_minutes + 1)
    token = create_access_token(settings, user_id=uuid.uuid4(), session_id=uuid.uuid4(), now=old)

    with pytest.raises(InvalidTokenError):
        decode_access_token(settings, token)


def test_baska_sirla_imzali_jeton_reddedilir(make_settings: SettingsFactory) -> None:
    other = make_settings(jwt_secret="baska-bir-sir-" + "y" * 32)
    token = create_access_token(other, user_id=uuid.uuid4(), session_id=uuid.uuid4(), now=NOW)

    with pytest.raises(InvalidTokenError):
        decode_access_token(make_settings(), token)


def _forge(settings_secret: str, **overrides: object) -> str:
    payload: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "typ": "access",
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": NOW,
        "exp": NOW + timedelta(minutes=5),
    }
    payload.update(overrides)
    return jwt.encode(payload, settings_secret, algorithm="HS256")


@pytest.mark.parametrize(
    "overrides",
    [{"typ": "refresh"}, {"aud": "baska-uygulama"}, {"iss": "baska"}, {"sub": "uuid-degil"}],
)
def test_yanlis_icerikli_jeton_reddedilir(
    make_settings: SettingsFactory, overrides: dict[str, object]
) -> None:
    settings = make_settings()
    token = _forge(settings.jwt_secret.get_secret_value(), **overrides)

    with pytest.raises(InvalidTokenError):
        decode_access_token(settings, token)


def test_imzasiz_none_algoritmali_jeton_reddedilir(make_settings: SettingsFactory) -> None:
    header = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"  # {"alg":"none","typ":"JWT"}
    body = jwt.utils.base64url_encode(json.dumps({"sub": str(uuid.uuid4())}).encode()).decode()

    with pytest.raises(InvalidTokenError):
        decode_access_token(make_settings(), f"{header}.{body}.")


def test_yenileme_jetonu_rastgele_ve_yalniz_hashi_saklanir() -> None:
    first, second = new_refresh_token(), new_refresh_token()

    assert first != second
    assert len(first) >= 43  # 256 bit
    assert hash_refresh_token(first) != first
    assert len(hash_refresh_token(first)) == 64
