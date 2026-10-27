"""Entegrasyon testlerinin kimlik yardımcıları (conftest de dışa aktarır)."""

import uuid
from typing import Any

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.roles import TEMPLATE_BY_NAME
from site_yonetim.models import SiteMembership, SiteRoleAssignment, User

DEFAULT_PASSWORD = "Gizli-Parola-2026"


async def create_user(
    factory: async_sessionmaker[AsyncSession],
    email: str,
    *,
    password: str = DEFAULT_PASSWORD,
    full_name: str = "Test KULLANICI",
    **fields: object,
) -> User:
    async with factory() as session, session.begin():
        user = User(
            email=email, password_hash=hash_password(password), full_name=full_name, **fields
        )
        session.add(user)
    return user


async def add_site_membership(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    **fields: object,
) -> None:
    """Üyelik + tesis kapsamlı rol ataması. `role`: şablon adı ("Yönetici") ya da rol anahtarı
    (`manager`, özel rolde `custom-…`). `**fields` üyeliğe gider (`is_active`, `person_id`)."""
    template = TEMPLATE_BY_NAME.get(role)
    with site_scope(site_id):
        async with factory() as session, session.begin():
            session.add(SiteMembership(user_id=user_id, **fields))
            session.add(
                SiteRoleAssignment(user_id=user_id, role_key=template.key if template else role)
            )


async def login_headers(
    client: httpx2.AsyncClient, email: str, password: str = DEFAULT_PASSWORD
) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """Hata gövdesi her istekte farklı `request_id` taşır; karşılaştırmada atılır."""
    error = {k: v for k, v in body["error"].items() if k != "request_id"}
    return {**body, "error": error}
