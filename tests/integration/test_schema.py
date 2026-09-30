"""Şema güvenceleri: her kiracı tablosunda RLS açık ve zorunlu; göçler modelle uyumlu."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from site_yonetim.db.base import Base, is_tenant_model
from site_yonetim.db.rls import POLICY_NAME
from tests.integration.conftest import DatabaseUrls

ROOT = Path(__file__).resolve().parents[2]

TENANT_TABLES = sorted(
    mapper.class_.__tablename__
    for mapper in Base.registry.mappers
    if is_tenant_model(mapper.class_)
)


async def test_her_kiraci_tablosunda_rls_acik_ve_zorunlu(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "       EXISTS (SELECT 1 FROM pg_policies p "
                    "               WHERE p.tablename = c.relname AND p.policyname = :policy) "
                    "FROM pg_class c WHERE c.relname = ANY(:tables)"
                ),
                {"tables": TENANT_TABLES, "policy": POLICY_NAME},
            )
        ).all()

    status = {name: (enabled, forced, has_policy) for name, enabled, forced, has_policy in rows}
    assert TENANT_TABLES, "kiracı tablosu bulunamadı"
    assert status == dict.fromkeys(TENANT_TABLES, (True, True, True))


def test_goc_ve_modeller_uyumlu(database_urls: DatabaseUrls, migrated_database: None) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_urls.admin.replace("%", "%%"))

    command.check(config)  # model değişip göç yazılmadıysa hata verir
