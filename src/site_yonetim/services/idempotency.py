"""Idempotency-Key — para yazan isteklerin tekrarında çift kayıt olmaz (docs/06 §1.5).

Aynı kullanıcı, aynı sitede, aynı anahtarla aynı isteği tekrar gönderirse **ilk yanıt** döner,
yeni kayıt açılmaz. Yanıt, yazmayla **aynı transaction'da** saklanır: yazma geri alınırsa anahtar
da kalmaz. Aynı anahtar farklı bir istekle kullanılırsa reddedilir. Anahtarlar 24 saat geçerli.

Aynı anahtarla eşzamanlı iki istek: ikisi de kaydı bulamaz, ikincisi benzersizlik kısıtında
düşer ve hiçbir şey yazmaz (uç 409 döner).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.models import IdempotencyKey

HEADER = "Idempotency-Key"
TTL = timedelta(hours=24)
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")


def check_key(key: str) -> str:
    if not _KEY.match(key):
        raise FinanceRuleError(
            "invalid_idempotency_key",
            "Idempotency-Key 8–100 karakter olmalı; harf, rakam ve - _ . : içerebilir "
            "(ör. bir UUID).",
            field=HEADER,
        )
    return key


def fingerprint(method: str, path: str, body: str) -> str:
    return hashlib.sha256(f"{method}\n{path}\n{body}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Stored:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Idempotency:
    user_id: uuid.UUID
    key: str | None
    fingerprint: str

    async def replay(self, session: AsyncSession, now: datetime) -> Stored | None:
        """Aynı istek daha önce işlendiyse yanıtı; değilse `None`."""
        if self.key is None:
            return None
        row: IdempotencyKey | None = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == self.user_id, IdempotencyKey.key == self.key
            )
        )
        if row is None:
            return None
        if now - row.created_at >= TTL:
            await session.delete(row)
            await session.flush()
            return None
        if row.fingerprint != self.fingerprint:
            raise FinanceRuleError(
                "idempotency_key_reused",
                "Bu Idempotency-Key başka bir istek için kullanıldı; her yeni işlem için yeni "
                "bir anahtar üretin.",
                field=HEADER,
            )
        return Stored(row.status_code, row.response_body)

    def remember(self, session: AsyncSession, status_code: int, body: dict[str, Any]) -> None:
        if self.key is not None:
            session.add(
                IdempotencyKey(
                    user_id=self.user_id,
                    key=self.key,
                    fingerprint=self.fingerprint,
                    status_code=status_code,
                    response_body=body,
                )
            )
