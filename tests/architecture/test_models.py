"""Model kuralları (docs/07-test-senaryolari.md §7) — veritabanı gerektirmez.

7.1 Her model ya `site_id` taşır ya da global beyaz listededir.
7.2 Beyaz listedeki global tablolar gerçekten `site_id` taşımaz.
7.3 Para/alan/oran sütunları kayan nokta değildir.
"""

import pytest
from sqlalchemy import Float, ForeignKeyConstraint, Numeric, Table

import site_yonetim.models  # noqa: F401 — tüm modeller kaydolsun
from site_yonetim.db.base import Base, is_tenant_model

# docs/03-veri-modeli.md [G] işaretli tablolar. Yeni global tablo = bilinçli karar + doküman.
GLOBAL_TABLES = {
    "plans",
    "organizations",
    "sites",
    "users",
    "organization_memberships",
    "auth_sessions",  # oturum kullanıcıya aittir, siteye değil
    "login_throttle",  # IP bazlı giriş hız sınırı; giriş site bağlamından önce
    "rate_limits",  # herkese açık uçların istek sınırı; site bağlamından önce
}

TABLES: list[Table] = list(Base.metadata.sorted_tables)
MAPPERS = list(Base.registry.mappers)


def test_modeller_kayitli() -> None:
    assert {"sites", "blocks", "units"} <= {table.name for table in TABLES}


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_7_1_her_tablo_ya_kiraci_ya_global(table: Table) -> None:
    if table.name in GLOBAL_TABLES:
        return
    site_id = table.columns.get("site_id")
    assert site_id is not None, f"{table.name}: site_id yok ve global listede değil"
    assert not site_id.nullable
    assert any(index.columns.keys()[:1] == ["site_id"] for index in table.indexes)


@pytest.mark.parametrize(
    "table", [t for t in TABLES if t.name in GLOBAL_TABLES], ids=lambda t: t.name
)
def test_7_2_global_tablolarda_site_id_yok(table: Table) -> None:
    assert "site_id" not in table.columns


@pytest.mark.parametrize("mapper", MAPPERS, ids=lambda m: m.class_.__name__)
def test_kiraci_modeli_tenant_mixin_tasir(mapper: object) -> None:
    table = mapper.local_table  # type: ignore[attr-defined]
    assert is_tenant_model(mapper.class_) == (table.name not in GLOBAL_TABLES)  # type: ignore[attr-defined]


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_7_3_kayan_nokta_sutun_yok(table: Table) -> None:
    for column in table.columns:
        assert not isinstance(column.type, Float), f"{table.name}.{column.name} Float"
        if isinstance(column.type, Numeric):
            assert column.type.asdecimal, f"{table.name}.{column.name} float döner"


@pytest.mark.parametrize(
    "table", [t for t in TABLES if t.name not in GLOBAL_TABLES], ids=lambda t: t.name
)
def test_kiraci_tablolari_arasi_yabanci_anahtar_site_bilesik(table: Table) -> None:
    for constraint in table.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        target = constraint.referred_table.name
        if target in GLOBAL_TABLES:
            continue
        assert "site_id" in constraint.column_keys, (
            f"{table.name} → {target}: kiracı tabloları arası FK (site_id, …) bileşik olmalı"
        )
