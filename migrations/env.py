"""Alembic ortamı. Göçler tablo sahibi rolle çalışır (DATABASE_ADMIN_URL)."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import site_yonetim.models  # noqa: F401 — tüm modeller metadata'ya kaydolsun
from site_yonetim.core.config import get_settings
from site_yonetim.db.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # Testler URL'yi doğrudan config üzerinden verir.
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    settings = get_settings()
    secret = settings.database_admin_url or settings.database_url
    if secret is None:
        raise RuntimeError("DATABASE_ADMIN_URL tanımlı değil. env.example dosyasına bakın.")
    return secret.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        {"sqlalchemy.url": _database_url()}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
