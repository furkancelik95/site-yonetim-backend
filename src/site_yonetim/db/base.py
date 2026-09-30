"""Model tabanı (docs/03-veri-modeli.md — Genel kurallar).

- Birincil anahtar zaman sıralı UUIDv7 (indeks yerelliği).
- Her tabloda `created_at` (NOT NULL, varsayılan now()) ve `updated_at`.
- Kiracı tabloları `TenantMixin` taşır: `site_id NOT NULL` + indeks + `(site_id, id)` benzersiz.
  Kiracı tabloları arası yabancı anahtarlar `(site_id, x_id)` bileşiktir — bir sitenin kaydı
  veritabanı düzeyinde başka sitenin kaydına bağlanamaz.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, MetaData, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class TenantMixin:
    """Kiracı (site) tablosu. `site_id` elle verilmez; açık site kapsamından damgalanır."""

    __tenant__: ClassVar[bool] = True

    @declared_attr
    def site_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805 — declared_attr sınıf alır
        return mapped_column(ForeignKey("sites.id"), index=True)


def tenant_table_args(*args: Any) -> tuple[Any, ...]:
    """Kiracı tablosunun `__table_args__`'ı: `(site_id, id)` benzersiz + verilen kısıtlar."""
    return (UniqueConstraint("site_id", "id"), *args)


def tenant_fk(column: str, target_table: str) -> ForeignKeyConstraint:
    """Aynı siteye bağlı bileşik yabancı anahtar: (site_id, column) → target(site_id, id)."""
    return ForeignKeyConstraint(
        ["site_id", column], [f"{target_table}.site_id", f"{target_table}.id"]
    )


def is_tenant_model(cls: type) -> bool:
    return isinstance(cls, type) and issubclass(cls, TenantMixin)


def enum_check(column: str, values: type[StrEnum]) -> str:
    """Enum sütunu `TEXT + CHECK` (docs/03 — Genel kurallar): "usage IN ('a', 'b')"."""
    allowed = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({allowed})"
