"""Platform işlemleri: müşteri (yönetim şirketi) açma — docs/06 §2.2.

Müşteri açma tek transaction'dır: organizasyon + ilk yetkili kullanıcı (Sahip) + şirket
üyeliği. Geçici parola **yalnız bir kez** döner; saklanan tek şey argon2id hash'idir.
"""

import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.security import hash_password
from site_yonetim.domain.access import OrganizationRole
from site_yonetim.models import Organization, OrganizationMembership, Plan, User

TEMPORARY_PASSWORD_BYTES = 12  # 16 karakterlik URL-güvenli metin


class PlatformRuleError(Exception):
    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class CreatedCustomer:
    organization: Organization
    admin: User
    temporary_password: str  # yalnız yanıtta; saklanmaz, loglanmaz


async def require_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise PlatformRuleError("plan_not_found", "plan_id", "Seçilen plan bulunamadı.")
    return plan


async def create_customer(
    session: AsyncSession,
    *,
    name: str,
    tax_number: str | None,
    plan_id: uuid.UUID,
    admin_full_name: str,
    admin_email: str,
) -> CreatedCustomer:
    await require_plan(session, plan_id)
    if await session.scalar(select(User.id).where(User.email == admin_email)):
        raise PlatformRuleError(
            "email_already_exists",
            "admin_email",
            "Bu e-posta ile kayıtlı bir kullanıcı zaten var.",
        )

    temporary_password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
    organization = Organization(name=name, tax_number=tax_number, plan_id=plan_id)
    admin = User(
        email=admin_email,
        password_hash=hash_password(temporary_password),
        full_name=admin_full_name,
        must_change_password=True,  # ilk girişte değiştirmek zorunda (docs/05 §8.1)
    )
    session.add_all([organization, admin])
    await session.flush()
    session.add(
        OrganizationMembership(
            organization_id=organization.id,
            user_id=admin.id,
            role=OrganizationRole.OWNER.value,
        )
    )
    await session.flush()
    return CreatedCustomer(organization, admin, temporary_password)
