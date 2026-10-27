"""Devriye — servis isteği 46 (Yazılım Dokümanı GUV-09). KİRACI tabloları.

Yürütme planın o anki hâlini dondurur (`point_ids`, `ordered`, `allowed_minutes`): plan sonradan
değişse de raporu değişmez. Bir planın aynı anda tek açık yürütmesi olur (kısmi tekil indeks).
Okutmada yalnız zaman tutulur; konum takibi yok.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.patrol import PatrolTag


class PatrolPoint(TenantMixin, Base):
    __tablename__ = "patrol_points"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "code"),
        UniqueConstraint("site_id", "name_key"),
        CheckConstraint(enum_check("tag", PatrolTag), name="tag"),
        CheckConstraint("code ~ '^[0-9]{6}$'", name="code"),
    )

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    tag: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)  # etikette basılı; gizli değil
    is_active: Mapped[bool] = mapped_column(default=True)


class PatrolPlan(TenantMixin, Base):
    __tablename__ = "patrol_plans"
    __table_args__ = tenant_table_args(
        CheckConstraint("allowed_minutes BETWEEN 5 AND 240", name="allowed_minutes"),
    )

    name: Mapped[str] = mapped_column(Text)
    point_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))  # sırayla
    ordered: Mapped[bool] = mapped_column(default=True)
    start_times: Mapped[list[str]] = mapped_column(ARRAY(Text))  # "22:00"
    allowed_minutes: Mapped[int]
    assignee: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class PatrolRun(TenantMixin, Base):
    __tablename__ = "patrol_runs"
    __table_args__ = tenant_table_args(
        tenant_fk("plan_id", "patrol_plans"),
        Index(
            "uq_patrol_runs_open_plan",
            "site_id",
            "plan_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index("ix_patrol_runs_site_day", "site_id", "run_date"),
    )

    plan_id: Mapped[uuid.UUID]
    plan_name: Mapped[str] = mapped_column(Text)
    point_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    ordered: Mapped[bool]
    allowed_minutes: Mapped[int]
    slot: Mapped[str | None] = mapped_column(Text)
    run_date: Mapped[dt.date] = mapped_column(Date)  # tesis yerel günü
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    started_by: Mapped[str] = mapped_column(Text)
    started_by_user_id: Mapped[uuid.UUID]
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_by: Mapped[str | None] = mapped_column(Text)


class PatrolScan(TenantMixin, Base):
    __tablename__ = "patrol_scans"
    __table_args__ = tenant_table_args(
        tenant_fk("run_id", "patrol_runs"),
        tenant_fk("point_id", "patrol_points"),
        UniqueConstraint("site_id", "run_id", "point_id"),
    )

    run_id: Mapped[uuid.UUID]
    point_id: Mapped[uuid.UUID]
    scanned_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    scanned_by: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    out_of_order: Mapped[bool] = mapped_column(default=False)
