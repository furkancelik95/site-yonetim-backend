"""Uygulama fabrikası. Çalıştırma: `uvicorn site_yonetim.main:app`."""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from site_yonetim.api.v1.router import API_V1_PREFIX, api_router
from site_yonetim.core.config import Settings, get_settings
from site_yonetim.core.errors import install_error_handlers
from site_yonetim.core.logging import configure_logging
from site_yonetim.core.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from site_yonetim.db.session import create_engine_from_settings, create_session_factory
from site_yonetim.seed.demo import seed_demo
from site_yonetim.services.imports import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

OPENAPI_URL = f"{API_V1_PREFIX}/openapi.json"
DOCS_URL = f"{API_V1_PREFIX}/docs"

# JSON istekleri için 1 MB bol bol yeter; Excel yüklemesine dosya + multipart payı.
MAX_BODY_BYTES = 1024 * 1024
_UPLOAD_LIMITS = (
    (re.compile(rf"{API_V1_PREFIX}/sites/[^/]+/imports/units"), MAX_UPLOAD_BYTES + 64 * 1024),
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Veritabanı tanımlıysa motor açılışta kurulur, kapanışta bırakılır."""
    settings: Settings = app.state.settings
    if settings.database_url is not None:
        engine = create_engine_from_settings(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        if settings.demo_data_allowed:  # yalnız development + SEED_DEMO_DATA=true
            try:
                await seed_demo(settings, app.state.session_factory)
            except Exception:
                logger.exception("Demo verisi yüklenemedi (göçler çalıştırıldı mı?)")
    try:
        yield
    finally:
        if app.state.engine is not None:
            await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title="Site Yönetim API",
        version="0.1.0",
        # Frontend tipleri OpenAPI şemasından üretir (docs/06-api-sozlesmesi.md).
        openapi_url=OPENAPI_URL if docs_enabled else None,
        docs_url=DOCS_URL if docs_enabled else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.engine = None
    app.state.session_factory = None

    install_error_handlers(app)
    app.include_router(api_router)

    # Sıra: en son eklenen en dışta çalışır.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES, overrides=_UPLOAD_LIMITS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # yenileme jetonu httpOnly çerezde (docs/05-yetki.md §8)
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.is_production,
        docs_paths=frozenset({DOCS_URL}),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_app()
