"""PostgreSQL Row Level Security — izolasyonun ikinci katmanı (docs/02-mimari.md §3).

Her kiracı tablosu göçte `enable_tenant_rls(op, "tablo")` ile korunur. Politika, işlem
başında uygulamanın ayarladığı `app.site_id` / `app.all_sites` değişkenlerine bakar
(`db/tenancy.py`). Değişken yoksa hiçbir satır görünmez ve yazılamaz (kapalı başarısızlık).

`FORCE ROW LEVEL SECURITY` tablo sahibini de politikaya tabi kılar. Süper kullanıcı ve
BYPASSRLS rolleri yine de atlar; uygulama bu rollerle bağlanmaz.
"""

import re
from typing import Protocol

POLICY_NAME = "tenant_isolation"

_PREDICATE = (
    "current_setting('app.all_sites', true) = 'on' "
    "OR site_id = NULLIF(current_setting('app.site_id', true), '')::uuid"
)


_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class _Operations(Protocol):
    def execute(self, sqltext: str) -> object: ...


def _checked(table: str) -> str:
    if not _TABLE_NAME.match(table):
        raise ValueError(f"Geçersiz tablo adı: {table!r}")
    return table


def enable_tenant_rls(op: _Operations, table: str) -> None:
    table = _checked(table)
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {POLICY_NAME} ON "{table}" USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})'
    )


def disable_tenant_rls(op: _Operations, table: str) -> None:
    table = _checked(table)
    op.execute(f'DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
