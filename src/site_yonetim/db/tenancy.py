"""Kiracı (site) izolasyonu — uygulama katmanı (docs/02-mimari.md §3, docs/07 §6).

İzolasyon iki katmanlıdır; biri atlanırsa diğeri tutar:

1. **Bu modül:** açık site kapsamında her ORM sorgusuna `site_id = :site` eklenir; yeni kayda
   site otomatik damgalanır; başka sitenin `site_id`'siyle yazmak ya da başka sitenin kaydını
   güncellemek/silmek `TenantLeakError` fırlatır; kapsam yokken kiracı tablosuna dokunmak
   `TenantScopeError` fırlatır. Filtre varsayılandır; geliştirici eklemeyi unutamaz.
2. **PostgreSQL RLS:** her işlemde `app.site_id` / `app.all_sites` ayarlanır; politika
   (`db/rls.py`) ham SQL'de bile başka sitenin satırını döndürmez.

Bir oturum tek bir kapsama **sabitlenir**: oturumun kimlik haritasında başka sitenin nesnesi
bulunamaz (`session.get` SQL atmadan önbellekten dönebildiği için bu şart). Başka site için
yeni oturum açılır.

"Tüm siteler" kapsamı (platform paneli, portföy, gece işleri) yalnız `all_sites_scope()` ile,
bilinçli olarak açılır; varsayılan asla "tüm siteler" değildir.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction, with_loader_criteria
from sqlalchemy.orm.attributes import instance_state

from site_yonetim.core.logging import site_id_var
from site_yonetim.db.base import TenantMixin, is_tenant_model


class TenantScopeError(RuntimeError):
    """Site bağlamı yokken ya da oturum başka bir kapsama sabitliyken kiracı verisine erişim."""


class TenantLeakError(RuntimeError):
    """Başka bir sitenin verisine yazma girişimi."""


@dataclass(frozen=True, slots=True)
class TenantScope:
    site_id: uuid.UUID | None
    all_sites: bool = False


_scope: ContextVar[TenantScope | None] = ContextVar("tenant_scope", default=None)
_PIN_KEY = "tenant_scope"


def current_scope() -> TenantScope | None:
    return _scope.get()


@contextmanager
def site_scope(site_id: uuid.UUID) -> Iterator[TenantScope]:
    """Tek bir sitenin kapsamını açar. İç içe açılabilir; kapanınca önceki kapsam geri gelir."""
    if not isinstance(site_id, uuid.UUID):
        raise TypeError("site_id UUID olmalı.")
    scope = TenantScope(site_id=site_id)
    token = _scope.set(scope)
    log_token = site_id_var.set(str(site_id))
    try:
        yield scope
    finally:
        site_id_var.reset(log_token)
        _scope.reset(token)


@contextmanager
def all_sites_scope() -> Iterator[TenantScope]:
    """Bütün siteleri gören kapsam. Yalnız platform paneli, portföy ve gece işleri için."""
    scope = TenantScope(site_id=None, all_sites=True)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


# --- Oturum sabitleme ve RLS değişkeni -------------------------------------


def _pin(session: Session, scope: TenantScope) -> None:
    pinned: TenantScope | None = session.info.get(_PIN_KEY)
    if pinned is None:
        session.info[_PIN_KEY] = scope
        if session.in_transaction():
            _apply_rls(session.connection(), scope)
    elif pinned != scope:
        raise TenantScopeError(
            "Bu veritabanı oturumu başka bir site kapsamına bağlı. "
            "Farklı bir site için yeni oturum açın."
        )


def _apply_rls(connection: Connection, scope: TenantScope) -> None:
    connection.execute(
        text(
            "SELECT set_config('app.site_id', :site_id, true), "
            "set_config('app.all_sites', :all_sites, true)"
        ),
        {
            "site_id": str(scope.site_id) if scope.site_id else "",
            "all_sites": "on" if scope.all_sites else "off",
        },
    )


def _touches_tenant_table(state: ORMExecuteState) -> bool:
    return any(is_tenant_model(mapper.class_) for mapper in state.all_mappers)


def _on_execute(state: ORMExecuteState) -> None:
    scope = current_scope()
    session = state.session

    if scope is None:
        if _touches_tenant_table(state):
            raise TenantScopeError("Site bağlamı açılmadan kiracı verisine erişilemez.")
        return

    _pin(session, scope)

    if scope.all_sites:
        return
    if (state.is_select or state.is_update or state.is_delete) and not (
        state.is_column_load or state.is_relationship_load
    ):
        site_id = scope.site_id
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantMixin,
                lambda cls: cls.site_id == site_id,
                include_aliases=True,
            )
        )


def _on_before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    tenant_new = [obj for obj in session.new if isinstance(obj, TenantMixin)]
    tenant_changed = [
        obj for obj in (*session.dirty, *session.deleted) if isinstance(obj, TenantMixin)
    ]
    if not tenant_new and not tenant_changed:
        return

    scope = current_scope()
    if scope is None:
        raise TenantScopeError("Site bağlamı açılmadan kiracı tablosuna yazılamaz.")
    _pin(session, scope)

    for obj in tenant_new:
        if scope.all_sites:
            if obj.site_id is None:
                raise TenantScopeError(
                    "Tüm siteler kapsamında yeni kayda site_id açıkça verilmeli."
                )
        elif obj.site_id is None:
            obj.site_id = scope.site_id
        elif obj.site_id != scope.site_id:
            raise TenantLeakError(
                "Veri sızıntısı engellendi: başka bir sitenin kimliğiyle kayıt eklenemez."
            )

    if scope.all_sites:
        return
    for obj in tenant_changed:
        original = _original_site_id(obj)
        if obj.site_id != scope.site_id or original != scope.site_id:
            raise TenantLeakError(
                "Veri sızıntısı engellendi: başka bir sitenin kaydı değiştirilemez."
            )


def _original_site_id(obj: TenantMixin) -> Any:
    history = instance_state(obj).attrs.site_id.history
    if history.deleted:
        return history.deleted[0]
    return obj.site_id


def _on_after_begin(session: Session, _tx: SessionTransaction, connection: Connection) -> None:
    pinned: TenantScope | None = session.info.get(_PIN_KEY)
    if pinned is not None:
        _apply_rls(connection, pinned)


class TenantSession(Session):
    """Kiracı korumalı oturum. Uygulamadaki her oturum bu sınıftan üretilir (`db/session.py`)."""


event.listen(TenantSession, "do_orm_execute", _on_execute)
event.listen(TenantSession, "before_flush", _on_before_flush)
event.listen(TenantSession, "after_begin", _on_after_begin)
