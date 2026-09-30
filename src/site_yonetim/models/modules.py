"""Sitede modül durumu — docs/03-veri-modeli.md §1 `site_modules` [K]."""

from typing import Any

from sqlalchemy import CheckConstraint, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_table_args
from site_yonetim.domain.modules import ModuleKey


class SiteModule(TenantMixin, Base):
    __tablename__ = "site_modules"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "module_key"),
        CheckConstraint(enum_check("module_key", ModuleKey), name="module_key"),
    )

    module_key: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=False)
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
