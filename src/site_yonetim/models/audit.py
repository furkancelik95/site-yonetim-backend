"""Denetim kaydı (audit log) — docs/09 §6. KİRACI tablosu, **değişmez** (tetikleyici).

**Tek yerden yazılır:** oturum olayı (`after_flush`), denetlenen modellerdeki her ekleme,
değişiklik ve silmeyi önce/sonra farkıyla kaydeder — uç noktalara elle eklenmez. Toplu içe
aktarma ve belge indirme gibi model değişikliği olmayan olaylar `record()` ile yazılır.

Kim yaptı: istek başında `set_actor` (kullanıcı, ad, IP). İstek dışı (demo, CLI) aktörsüzdür.

Kayıt iş olayı düzeyindedir: tahakkuk koşusu, tahsilat, gider, kasa hareketi tek satır; koşunun
ürettiği borç/hareket satırları (türetilmiş, değişmez) ayrıca yazılmaz.
"""

import datetime as dt
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, event, insert, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_table_args
from site_yonetim.db.tenancy import TenantSession


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    IMPORT = "import"
    DOWNLOAD = "download"


class AuditLog(TenantMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("action", AuditAction), name="action"),
        Index("ix_audit_log_site_created", "site_id", "created_at"),
        Index("ix_audit_log_entity", "site_id", "entity", "entity_id"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_name: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)  # tablo adı
    entity_id: Mapped[uuid.UUID | None]
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(Text)


# --- Aktör (kim) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID | None
    name: str | None
    ip: str | None


_actor: ContextVar[Actor | None] = ContextVar("audit_actor", default=None)


def set_actor(actor: Actor | None) -> None:
    _actor.set(actor)


def current_actor() -> Actor | None:
    return _actor.get()


# --- Denetlenen modeller --------------------------------------------------------------

# Tablo adı kümesi: modeller burada içe aktarılmaz (döngüsel içe aktarma olmasın).
AUDITED_TABLES = frozenset(
    {
        # finans — iş olayları
        "charge_runs",
        "payments",
        "expenses",
        "cash_movements",
        "cash_accounts",
        "budget_plans",
        "budget_items",
        "periods",
        "clearance_certificates",
        # ayarlar
        "charge_types",
        "allocation_rules",
        "allocation_components",
        "late_fee_policies",
        "site_modules",
        # yetki / üyelik
        "site_memberships",
    }
)
_SKIPPED_COLUMNS = frozenset({"id", "site_id", "created_at", "updated_at"})
_PENDING = "audit_pending"


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _values(obj: Any) -> dict[str, Any]:
    state = inspect(obj)
    return {
        column.key: _json(getattr(obj, column.key))
        for column in state.mapper.column_attrs
        if column.key not in _SKIPPED_COLUMNS
    }


def _changes(obj: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        if attr.key in _SKIPPED_COLUMNS:
            continue
        history = state.attrs[attr.key].history
        if history.has_changes():
            before[attr.key] = _json(history.deleted[0]) if history.deleted else None
            after[attr.key] = _json(history.added[0]) if history.added else None
    return before, after


def _audited(obj: Any) -> bool:
    return getattr(obj, "__tablename__", None) in AUDITED_TABLES


def _on_after_flush(session: Session, _context: Any) -> None:
    pending: list[tuple[AuditAction, Any, dict[str, Any] | None, dict[str, Any] | None]] = (
        session.info.setdefault(_PENDING, [])
    )
    for obj in session.new:
        if _audited(obj):
            pending.append((AuditAction.CREATE, obj, None, _values(obj)))
    for obj in session.dirty:
        if _audited(obj) and session.is_modified(obj, include_collections=False):
            before, after = _changes(obj)
            if after:
                pending.append((AuditAction.UPDATE, obj, before, after))
    for obj in session.deleted:
        if _audited(obj):
            pending.append((AuditAction.DELETE, obj, _values(obj), None))


def _row(
    action: AuditAction,
    entity: str,
    entity_id: uuid.UUID | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    actor = current_actor()
    return {
        "user_id": actor.user_id if actor else None,
        "actor_name": actor.name if actor else None,
        "action": action.value,
        "entity": entity,
        "entity_id": entity_id,
        "before": before,
        "after": after,
        "ip": actor.ip if actor else None,
    }


def _on_after_flush_postexec(session: Session, _context: Any) -> None:
    """Aynı flush'ta, aynı bağlantı ve transaction'la yazılır (değişiklik geri alınırsa kayıt da).

    Denetlenen tablolar kiracı tablosudur: flush site kapsamında olmuştur, bağlantının RLS
    ayarı yapılmıştır. `site_id` kaydın kendisinden alınır — kapsam commit'ten önce kapanmış
    olabilir (ör. site açılışı), bu yüzden kayıt sonraki flush'a bırakılmaz.
    """
    pending = session.info.pop(_PENDING, [])
    if not pending:
        return
    rows = [
        _row(action, obj.__tablename__, obj.id, before, after) | {"site_id": obj.site_id}
        for action, obj, before, after in pending
    ]
    session.connection().execute(insert(AuditLog), rows)


def record(
    session: Session | AsyncSession,
    action: AuditAction,
    entity: str,
    entity_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Model değişikliği olmayan olay (toplu aktarım, belge indirme); aynı transaction'da."""
    after = {k: _json(v) for k, v in (detail or {}).items()}
    session.add(AuditLog(**_row(action, entity, entity_id, None, after)))


event.listen(TenantSession, "after_flush", _on_after_flush)
event.listen(TenantSession, "after_flush_postexec", _on_after_flush_postexec)
