"""Gerçek PostgreSQL ile entegrasyon testleri (docs/07-test-senaryolari.md §6).

Gerekenler (ayrıntı: README):
  TEST_DATABASE_URL        uygulama rolü (site_yonetim_app) — RLS'e tabidir
  TEST_DATABASE_ADMIN_URL  tablo sahibi (site_yonetim_owner) — göç ve temizlik

Tanımlı değilse testler atlanır; CI'da (CI=true) atlanmaz, kırılır.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from site_yonetim.db.base import Base
from site_yonetim.db.session import create_session_factory
from site_yonetim.db.tenancy import site_scope
from site_yonetim.main import create_app
from site_yonetim.models import Block, Site, Unit
from tests.conftest import SettingsFactory
from tests.integration.finance_world import world
from tests.integration.helpers import (
    DEFAULT_PASSWORD,
    add_site_membership,
    create_user,
    login_headers,
)

__all__ = ["DEFAULT_PASSWORD", "add_site_membership", "create_user", "login_headers", "world"]

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DatabaseUrls:
    app: str
    admin: str


@pytest.fixture(scope="session")
def database_urls() -> DatabaseUrls:
    app_url = os.environ.get("TEST_DATABASE_URL")
    admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if not app_url or not admin_url:
        message = "TEST_DATABASE_URL / TEST_DATABASE_ADMIN_URL tanımlı değil"
        if os.environ.get("CI"):
            pytest.fail(f"{message}: CI'da entegrasyon testleri atlanamaz.")
        pytest.skip(message)
    return DatabaseUrls(app=app_url, admin=admin_url)


@pytest.fixture(scope="session")
def migrated_database(database_urls: DatabaseUrls) -> None:
    """Şemayı sıfırdan kurar: downgrade base → upgrade head (geri dönüş yolu da sınanır)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_urls.admin.replace("%", "%%"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
async def admin_engine(
    database_urls: DatabaseUrls, migrated_database: None
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_urls.admin, poolclass=NullPool)
    yield engine
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    await engine.dispose()


@pytest.fixture
async def app_engine(
    database_urls: DatabaseUrls, admin_engine: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_urls.app, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(app_engine)


@dataclass(frozen=True)
class TwoSites:
    """docs/07 §6 kurgusu: A (1 blok, 4 daire) · B (1 blok, 3 daire)."""

    site_a: uuid.UUID
    site_b: uuid.UUID
    block_a: uuid.UUID
    block_b: uuid.UUID
    units_a: tuple[uuid.UUID, ...]
    units_b: tuple[uuid.UUID, ...]


async def _seed_site(
    factory: async_sessionmaker[AsyncSession], site_id: uuid.UUID, block_name: str, units: int
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    with site_scope(site_id):
        async with factory() as session, session.begin():
            block = Block(name=block_name)
            session.add(block)
            await session.flush()
            rows = [Unit(block_id=block.id, number=str(n)) for n in range(1, units + 1)]
            session.add_all(rows)
        return block.id, tuple(row.id for row in rows)


@pytest.fixture
async def two_sites(session_factory: async_sessionmaker[AsyncSession]) -> TwoSites:
    site_a = Site(name="Aksu Konakları", slug="aksu-konaklari")
    site_b = Site(name="Yıldız Sitesi", slug="yildiz-sitesi")
    async with session_factory() as session, session.begin():
        session.add_all([site_a, site_b])

    block_a, units_a = await _seed_site(session_factory, site_a.id, "Aksu A", 4)
    block_b, units_b = await _seed_site(session_factory, site_b.id, "Yıldız A", 3)
    return TwoSites(site_a.id, site_b.id, block_a, block_b, units_a, units_b)


# --- Kimlik yardımcıları (tests/integration/helpers.py) ------------------------


@pytest.fixture
def api_app(
    make_settings: SettingsFactory, session_factory: async_sessionmaker[AsyncSession]
) -> FastAPI:
    app = create_app(
        make_settings(refresh_cookie_secure=False, cors_origins=["http://localhost:5173"])
    )
    app.state.session_factory = session_factory
    return app


@pytest.fixture
async def api(api_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=api_app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
