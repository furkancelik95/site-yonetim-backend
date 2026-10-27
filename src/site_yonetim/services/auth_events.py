"""Kimlik olayı yazımı ve istemci bilgisi (Yazılım Dokümanı M-24, PLT-10) — kimlik
servislerinin ortak parçası."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.logging import request_id_var
from site_yonetim.models import AuthEvent, AuthEventKind


@dataclass(frozen=True, slots=True)
class Client:
    """İsteğin geldiği yer: oturum listesi ve kimlik olayı için (Yazılım Dokümanı PLT-10, M-24)."""

    ip: str | None = None
    user_agent: str | None = None


NO_CLIENT = Client()


def event(
    session: AsyncSession,
    kind: AuthEventKind,
    client: Client,
    *,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> None:
    """Kimlik olayını aynı transaction'a ekler; çağıran commit eder."""
    session.add(
        AuthEvent(
            user_id=user_id,
            kind=kind.value,
            session_id=session_id,
            ip=client.ip,
            user_agent=client.user_agent,
            request_id=request_id_var.get(),
        )
    )
