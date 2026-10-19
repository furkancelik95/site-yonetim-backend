"""Onay mekanizması — Yazılım Dokümanı Bölüm 6 (docs/03). KİRACI tabloları.

- Kural **versiyonludur**: düzenleme yeni versiyon yazar (eski versiyon değişmez — tetikleyici).
  Talep açıldığı andaki versiyonla ilerler; bekleyen talepler kural değişikliğinden etkilenmez.
- Talep, kuralın o anki hâlinin **kopyasını** (`snapshot`) taşır: varsayılan kural da dahil, adım
  ve süre ayarları sonradan değişmez.
- Onaycılar adım açılırken çözülür (`approval_assignments`): talep eden listeden çıkar; aynı kişi
  aynı talepte yalnız bir kez karar verir (benzersiz kısıt).
- Kararlar **değişmez** (tetikleyici); revizyonda talep yeni sürüme geçer, eski kararlar kalır.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.approvals import (
    ActionType,
    DayBasis,
    Decision,
    RejectBehavior,
    RequestStatus,
    StepMode,
)
from site_yonetim.models.finance import MONEY


class ApprovalRule(TenantMixin, Base):
    """Kural kimliği; içerik versiyonlarda. Arşivlenen kural yeni taleplere uygulanmaz."""

    __tablename__ = "approval_rules"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("action_type", ActionType), name="action_type"),
        Index("ix_approval_rules_site_action", "site_id", "action_type"),
    )

    action_type: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_template: Mapped[bool] = mapped_column(default=False)  # "Önerilen" hazır şablondan
    current_version: Mapped[int] = mapped_column(default=1)


class ApprovalRuleVersion(TenantMixin, Base):
    """Kuralın bir versiyonu — **değişmez**."""

    __tablename__ = "approval_rule_versions"
    __table_args__ = tenant_table_args(
        tenant_fk("rule_id", "approval_rules"),
        UniqueConstraint("rule_id", "version"),
        CheckConstraint(enum_check("mode", StepMode), name="mode"),
        CheckConstraint(enum_check("reject_behavior", RejectBehavior), name="reject_behavior"),
        CheckConstraint(enum_check("day_basis", DayBasis), name="day_basis"),
        CheckConstraint("remind_after_days BETWEEN 1 AND 30", name="remind"),
        CheckConstraint("escalate_after_days > remind_after_days", name="escalate"),
        CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_max > amount_min", name="range"
        ),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(index=True)
    version: Mapped[int]
    amount_min: Mapped[Decimal | None] = mapped_column(MONEY)  # hariç
    amount_max: Mapped[Decimal | None] = mapped_column(MONEY)  # dahil
    source: Mapped[str | None] = mapped_column(Text)  # ör. "emergency"
    mode: Mapped[str] = mapped_column(Text)
    reject_behavior: Mapped[str] = mapped_column(Text)
    remind_after_days: Mapped[int]
    escalate_after_days: Mapped[int]
    day_basis: Mapped[str] = mapped_column(Text, default=DayBasis.CALENDAR.value)
    escalation_role: Mapped[str | None] = mapped_column(Text)  # K20: seçilmezse yalnız hatırlatma
    effective_from: Mapped[dt.date] = mapped_column(Date)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)  # [{roles, quorum, count}]
    basis_note: Mapped[str | None] = mapped_column(Text)  # dayanak (ör. YK kararı no)
    created_by_name: Mapped[str | None] = mapped_column(Text)


class ApprovalRequest(TenantMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = tenant_table_args(
        tenant_fk("rule_version_id", "approval_rule_versions"),
        UniqueConstraint("site_id", "number"),
        CheckConstraint(enum_check("action_type", ActionType), name="action_type"),
        CheckConstraint(enum_check("status", RequestStatus), name="status"),
        Index("ix_approval_requests_site_status", "site_id", "status"),
        Index("ix_approval_requests_site_requester", "site_id", "requested_by"),
    )

    number: Mapped[int]
    action_type: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str | None] = mapped_column(Text)  # modülün kaydı (tablo adı)
    subject_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(Text)  # "Satın alma — Asansör halatı — 42.500,00 TL"
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # modülün özet kartı
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # yürütülecek komutun verisi
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    source: Mapped[str | None] = mapped_column(Text)
    post_approval: Mapped[bool] = mapped_column(default=False)  # "sonradan onay" (acil gider)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    requested_by_name: Mapped[str] = mapped_column(Text)
    rule_version_id: Mapped[uuid.UUID | None]  # boş: varsayılan kural
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)  # adımlar ve süreler (donmuş)
    status: Mapped[str] = mapped_column(Text, default=RequestStatus.PENDING.value)
    revision: Mapped[int] = mapped_column(default=1)
    current_step: Mapped[int] = mapped_column(default=0)
    step_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class ApprovalAssignment(TenantMixin, Base):
    """Adımın onaycısı (adım açılırken çözülür). `source`: rule · escalation."""

    __tablename__ = "approval_assignments"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "approval_requests"),
        UniqueConstraint("request_id", "revision", "step_index", "user_id"),
        CheckConstraint("source IN ('rule', 'escalation')", name="source"),
        Index("ix_approval_assignments_site_user", "site_id", "user_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    revision: Mapped[int]
    step_index: Mapped[int]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    user_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="rule")


class ApprovalDecision(TenantMixin, Base):
    """Karar — **değişmez**. Vekâleten verilen kararda `on_behalf_of` asıl onaycıdır."""

    __tablename__ = "approval_decisions"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "approval_requests"),
        CheckConstraint(enum_check("decision", Decision), name="decision"),
        # aynı kişi aynı sürümde bir kez karar verir; bir onaycının hakkı da bir kez kullanılır
        UniqueConstraint("request_id", "revision", "user_id"),
        UniqueConstraint("request_id", "revision", "slot_user_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    revision: Mapped[int]
    step_index: Mapped[int]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))  # kararı veren
    user_name: Mapped[str] = mapped_column(Text)
    slot_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))  # kimin hakkı
    on_behalf_of_name: Mapped[str | None] = mapped_column(Text)  # vekâleten: asıl onaycı
    decision: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)  # ret/revizyonda gerekçe zorunlu


class ApprovalDelegation(TenantMixin, Base):
    """Yetki devri (ONY-03): tarih-saat aralıklı, rol seçimli. Zincirleme devir yok; aynı rol için
    aynı anda tek aktif devir. K20'ye kadar devralan **aynı** role sahip olmalı."""

    __tablename__ = "approval_delegations"
    __table_args__ = tenant_table_args(
        CheckConstraint("ends_at > starts_at", name="period"),
        CheckConstraint("from_user_id <> to_user_id", name="not_self"),
        Index("ix_approval_delegations_site_to", "site_id", "to_user_id"),
    )

    from_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    from_name: Mapped[str] = mapped_column(Text)
    to_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    to_name: Mapped[str] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # erken bitiş
    note: Mapped[str | None] = mapped_column(Text)
    start_notified: Mapped[bool] = mapped_column(default=False)
    end_notified: Mapped[bool] = mapped_column(default=False)
