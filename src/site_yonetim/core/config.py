"""Uygulama yapılandırması.

Her değer ortam değişkeninden okunur (docs/02-mimari.md §7). Sır koda girmez; yerelde
`env.example` → `.env` kopyalanır, `.env` repoya girmez.
"""

import tempfile
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# env.example'daki yer tutucu; sır değil. Üretimde bu değerle açılış reddedilir.
PLACEHOLDER_SECRET = "__SET_ME__"  # noqa: S105  # nosec B105
MIN_JWT_SECRET_LENGTH = 32


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    # Uygulama rolü: süper kullanıcı değil, BYPASSRLS yok (docs/02-mimari.md §3).
    database_url: SecretStr | None = None
    # Tablo sahibi: yalnız göçler (alembic) kullanır.
    database_admin_url: SecretStr | None = None
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    jwt_secret: SecretStr = SecretStr(PLACEHOLDER_SECRET)
    jwt_access_minutes: int = Field(default=15, ge=1, le=60)
    # Oturum ömrü: kayan pencere (docs/05 §8). Her yenilemede uzar.
    session_hours: int = Field(default=8, ge=1, le=24)
    # Yenileme jetonu çerezi: httpOnly + SameSite=Lax; üretimde Secure zorunlu.
    refresh_cookie_secure: bool = True
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )
    api_docs_enabled: bool = True
    seed_demo_data: bool = False
    # Onay bekleyen Excel aktarımları (docs/11 §1); boşsa sistemin geçici klasörü.
    # Birden çok API kopyası çalışıyorsa hepsinin gördüğü ortak bir birim olmalı.
    import_storage_dir: Path | None = None
    log_level: str = "INFO"

    @field_validator("cors_origins", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("CORS_ORIGINS '*' olamaz; frontend alan adını açıkça yazın.")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Self:
        if self.environment is not Environment.PRODUCTION:
            return self
        secret = self.jwt_secret.get_secret_value()
        if secret == PLACEHOLDER_SECRET or len(secret) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                f"Üretimde JWT_SECRET en az {MIN_JWT_SECRET_LENGTH} karakterlik "
                "gerçek bir sır olmalı."
            )
        if "*" in self.allowed_hosts:
            raise ValueError("Üretimde ALLOWED_HOSTS '*' olamaz.")
        if not self.refresh_cookie_secure:
            raise ValueError("Üretimde REFRESH_COOKIE_SECURE kapatılamaz (çerez yalnız HTTPS).")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def import_dir(self) -> Path:
        return self.import_storage_dir or Path(tempfile.gettempdir()) / "site-yonetim-imports"

    @property
    def demo_data_allowed(self) -> bool:
        """Demo verisi (herkesçe bilinen `Demo1234!` parolalı hesaplar) yalnız geliştirmede."""
        return self.seed_demo_data and self.environment is Environment.DEVELOPMENT


@lru_cache
def get_settings() -> Settings:
    return Settings()
