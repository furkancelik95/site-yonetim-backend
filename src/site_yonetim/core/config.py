"""Uygulama yapılandırması.

Her değer ortam değişkeninden okunur (docs/02-mimari.md §7). Sır koda girmez; yerelde
`env.example` → `.env` kopyalanır, `.env` repoya girmez.
"""

import tempfile
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

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
    # IP bazlı giriş hız sınırı (docs/09 §4): pencerede bu kadar hatalı denemeden sonra 429.
    # Ters vekil arkasında gerçek istemci adresi için FORWARDED_ALLOW_IPS (uvicorn) verilmeli.
    login_ip_max_failures: int = Field(default=20, ge=1, le=10_000)
    login_ip_window_minutes: int = Field(default=15, ge=1, le=24 * 60)
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
    # Yüklenen belgeler (fatura görüntüsü) — web kökünün DIŞINDA, kalıcı birim (docs/09 §3).
    file_storage_root: Path = Path("var/files")
    log_level: str = "INFO"
    # --- Kimlik ve bildirim gönderimi (Yazılım Dokümanı PLT-01, PLT-05, §4.7) ---
    # Sağlayıcı açık karar K5'te seçilecek; şimdilik yalnız test sağlayıcısı: gönderim kayda
    # geçer, dışarı gitmez. Gerçek sağlayıcı eklenince buraya yeni değer gelir.
    sms_provider: Literal["test"] = "test"
    email_provider: Literal["test"] = "test"
    push_provider: Literal["test"] = "test"
    # Test sağlayıcısının son mesajlarını (giriş kodları dahil) gösterme: platform yöneticisine
    # "test gelen kutusu" ve demo girişinde kodun ekranda görünmesi. Boşsa geliştirme/test
    # ortamında açık; ÜRETİMDE HİÇBİR KOŞULDA açılmaz (açılış reddedilir).
    test_inbox_enabled: bool | None = None
    # Davet ve bildirim bağlantılarının kökü (frontend adresi). Boşsa ilk CORS kaynağı.
    public_web_url: str | None = None
    # Yönetici rollerinde iki adımlı doğrulama zorunlu (§4.7). Geçiş süresi: kurmamış yönetici
    # ilk girişinden itibaren bu kadar gün uyarıyla girer; sonra yalnız kurulum ekranı açılır.
    # 0 → dokümandaki gibi anında zorunlu.
    mfa_grace_days: int = Field(default=14, ge=0, le=180)
    mfa_issuer: str = "SiteYönetimi"
    # Paket içi aylık SMS kotası (A-08: miktar pilot sonrası); site bazında ezilebilir.
    sms_default_monthly_quota: int = Field(default=1000, ge=0, le=10_000_000)
    # İki adımlı doğrulama sırrı ve sağlayıcı anahtarları bu anahtarla şifrelenir. Boşsa
    # JWT_SECRET'tan türetilir (JWT_SECRET değişirse kurulu doğrulayıcılar yeniden kurulur).
    data_encryption_key: SecretStr | None = None

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
        if self.test_inbox_enabled:
            raise ValueError(
                "Üretimde TEST_INBOX_ENABLED açılamaz (test sağlayıcısı kodları dışarı vermez)."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def test_inbox_active(self) -> bool:
        """Test sağlayıcısının mesajları (kodlar dahil) gösterilebilir mi? Üretimde asla."""
        if self.is_production:
            return False
        return True if self.test_inbox_enabled is None else self.test_inbox_enabled

    @property
    def web_url(self) -> str:
        base = self.public_web_url or (self.cors_origins[0] if self.cors_origins else "")
        return base.rstrip("/")

    @property
    def encryption_key(self) -> bytes:
        source = self.data_encryption_key or self.jwt_secret
        return source.get_secret_value().encode()

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
