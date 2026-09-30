"""Motor ve oturum fabrikası.

Uygulama veritabanına **süper kullanıcı olmayan, BYPASSRLS yetkisi olmayan** bir rolle
bağlanır (`DATABASE_URL`); tabloların sahibi ve göçleri çalıştıran rol ayrıdır
(`DATABASE_ADMIN_URL`). Süper kullanıcı RLS'i atlar — uygulama asla onunla bağlanmaz.
"""

import logging
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from site_yonetim.core.config import Settings
from site_yonetim.db.tenancy import TenantSession

SLOW_QUERY_MS = 300  # docs/02-mimari.md §9

logger = logging.getLogger("site_yonetim.db")


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL tanımlı değil. env.example dosyasına bakın.")
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        # Uzun süren sorgu bağlantıyı kilitlemesin.
        connect_args={"command_timeout": 30},
    )
    install_slow_query_log(engine.sync_engine)
    return engine


def install_slow_query_log(engine: Engine, threshold_ms: int = SLOW_QUERY_MS) -> None:
    """Eşiği aşan sorguyu loglar. Parametreler yazılmaz (kişisel veri içerebilir)."""

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn: Connection, *_: Any) -> None:
        conn.info.setdefault("query_started", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _finish(conn: Connection, _cursor: Any, statement: str, *_: Any) -> None:
        elapsed_ms = (time.perf_counter() - conn.info["query_started"].pop()) * 1000
        if elapsed_ms > threshold_ms:
            logger.warning("Yavaş sorgu (%.0f ms): %s", elapsed_ms, " ".join(statement.split()))


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        sync_session_class=TenantSession,
        expire_on_commit=False,
        autoflush=True,
    )
