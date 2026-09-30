from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from site_yonetim.core.config import Settings
from site_yonetim.main import create_app

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Geliştiricinin yerel `.env` dosyasından etkilenmeyen, testlere özel ayar."""

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "environment": "test",
            "jwt_secret": "test-jwt-sirri-" + "x" * 32,
            "cors_origins": ["http://localhost:5173"],
            "log_level": "WARNING",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
