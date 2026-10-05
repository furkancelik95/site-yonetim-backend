"""Kimlik ve yetki — saf kurallar (docs/05-yetki.md).

İlke: kod **izne** bakar, rol adına bakmaz (`access.can("finance.charge.post")`). Rol, izinlerin
bir kümesidir; yeni rol eklemek yalnız bu tabloya satır eklemektir.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Permission(StrEnum):
    FINANCE_READ = "finance.read"
    FINANCE_CHARGE_POST = "finance.charge.post"
    FINANCE_PAYMENT_RECORD = "finance.payment.record"
    FINANCE_BUDGET_MANAGE = "finance.budget.manage"
    FINANCE_CASH_READ = "finance.cash.read"
    FINANCE_CASH_MANAGE = "finance.cash.manage"
    FINANCE_REPORTS_READ = "finance.reports.read"
    UNITS_READ = "units.read"
    UNITS_MANAGE = "units.manage"
    PEOPLE_READ = "people.read"
    PEOPLE_MANAGE = "people.manage"
    REQUESTS_READ = "requests.read"
    REQUESTS_CREATE = "requests.create"
    REQUESTS_ASSIGN = "requests.assign"
    ANNOUNCEMENTS_READ = "announcements.read"
    ANNOUNCEMENTS_PUBLISH = "announcements.publish"
    EXPENSES_READ = "expenses.read"
    EXPENSES_MANAGE = "expenses.manage"
    SECURITY_VISITORS = "security.visitors"
    SECURITY_PACKAGES = "security.packages"
    MODULES_MANAGE = "modules.manage"
    MEMBERS_MANAGE = "members.manage"
    PORTFOLIO_READ = "portfolio.read"
    AUDIT_READ = "audit.read"
    # Servis istekleri 09–18 (karar: Furkan, 05.10.2026)
    SECURITY_INCIDENTS = "security.incidents"  # olay kaydı ve kayıp eşya
    MEETINGS_READ = "meetings.read"
    MEETINGS_MANAGE = "meetings.manage"
    POLLS_MANAGE = "polls.manage"  # okuma: announcements.read
    CONTRACTS_MANAGE = "contracts.manage"  # okuma: expenses.read
    INVENTORY_READ = "inventory.read"
    INVENTORY_MANAGE = "inventory.manage"
    STAFF_MANAGE = "staff.manage"  # okuma: people.read


class SiteRole(StrEnum):
    """Site rol şablonları. Adlar Türkçe saklanır (docs/05 §3)."""

    MANAGER = "Yönetici"
    BOARD_MEMBER = "Yönetim Kurulu Üyesi"
    AUDITOR = "Denetçi"
    ACCOUNTING = "Muhasebe"
    SECURITY = "Güvenlik"
    TECHNICIAN = "Teknik Personel"
    RESIDENT = "Sakin"


class OrganizationRole(StrEnum):
    OWNER = "Sahip"
    MANAGER = "Yönetici"
    ACCOUNTING = "Muhasebe"
    VIEWER = "İzleyici"


class UserKind(StrEnum):
    STAFF = "staff"
    RESIDENT = "resident"


P = Permission
ROLE_PERMISSIONS: dict[SiteRole, frozenset[Permission]] = {
    SiteRole.MANAGER: frozenset(
        {
            P.FINANCE_READ, P.FINANCE_CHARGE_POST, P.FINANCE_PAYMENT_RECORD,
            P.FINANCE_BUDGET_MANAGE, P.FINANCE_CASH_READ, P.FINANCE_CASH_MANAGE,
            P.FINANCE_REPORTS_READ, P.UNITS_READ, P.UNITS_MANAGE, P.PEOPLE_READ,
            P.PEOPLE_MANAGE, P.REQUESTS_READ, P.REQUESTS_CREATE, P.REQUESTS_ASSIGN,
            P.ANNOUNCEMENTS_READ, P.ANNOUNCEMENTS_PUBLISH, P.EXPENSES_READ, P.EXPENSES_MANAGE,
            P.SECURITY_VISITORS, P.SECURITY_PACKAGES, P.MODULES_MANAGE, P.MEMBERS_MANAGE,
            P.AUDIT_READ, P.SECURITY_INCIDENTS, P.MEETINGS_READ, P.MEETINGS_MANAGE,
            P.POLLS_MANAGE, P.CONTRACTS_MANAGE, P.INVENTORY_READ, P.INVENTORY_MANAGE,
            P.STAFF_MANAGE,
        }
    ),
    SiteRole.BOARD_MEMBER: frozenset(
        {
            P.FINANCE_READ, P.UNITS_READ, P.PEOPLE_READ, P.REQUESTS_READ, P.ANNOUNCEMENTS_READ,
            P.EXPENSES_READ, P.FINANCE_CASH_READ, P.FINANCE_REPORTS_READ, P.MEETINGS_READ,
            P.INVENTORY_READ,
        }
    ),
    # KMK m.41 — salt okunur finans, kişisel veri görmez
    SiteRole.AUDITOR: frozenset(
        {
            P.FINANCE_READ, P.EXPENSES_READ, P.AUDIT_READ, P.FINANCE_CASH_READ,
            P.FINANCE_REPORTS_READ, P.MEETINGS_READ,
        }
    ),
    SiteRole.ACCOUNTING: frozenset(
        {
            P.FINANCE_READ, P.FINANCE_CHARGE_POST, P.FINANCE_PAYMENT_RECORD,
            P.FINANCE_BUDGET_MANAGE, P.EXPENSES_READ, P.EXPENSES_MANAGE, P.FINANCE_CASH_READ,
            P.FINANCE_CASH_MANAGE, P.FINANCE_REPORTS_READ, P.UNITS_READ, P.PEOPLE_READ,
            P.CONTRACTS_MANAGE, P.INVENTORY_READ,
        }
    ),
    # Yalnızca kapıdaki iş: borç, kişi listesi, muhasebe yok
    SiteRole.SECURITY: frozenset(
        {
            P.SECURITY_VISITORS, P.SECURITY_PACKAGES, P.SECURITY_INCIDENTS, P.REQUESTS_CREATE,
            P.ANNOUNCEMENTS_READ,
        }
    ),
    SiteRole.TECHNICIAN: frozenset(
        {
            P.REQUESTS_READ, P.REQUESTS_ASSIGN, P.REQUESTS_CREATE, P.ANNOUNCEMENTS_READ,
            P.INVENTORY_READ, P.INVENTORY_MANAGE,
        }
    ),
    # + kendi dairesine ait okuma, person_id üzerinden (docs/05 §6)
    SiteRole.RESIDENT: frozenset({P.ANNOUNCEMENTS_READ, P.REQUESTS_CREATE}),
}  # fmt: skip

# Yönetim şirketi rolü → sitedeki karşılığı (docs/05 §4)
ORGANIZATION_ROLE_TO_SITE_ROLE: dict[OrganizationRole, SiteRole] = {
    OrganizationRole.OWNER: SiteRole.MANAGER,
    OrganizationRole.MANAGER: SiteRole.MANAGER,
    OrganizationRole.ACCOUNTING: SiteRole.ACCOUNTING,
    OrganizationRole.VIEWER: SiteRole.BOARD_MEMBER,
}


# --- Erişim çözümleme -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteRef:
    id: uuid.UUID
    name: str
    slug: str
    organization_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ExplicitMembership:
    site: SiteRef
    role: str
    is_active: bool
    person_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class OrganizationMembership:
    organization_id: uuid.UUID
    role: str
    is_active: bool
    sites: tuple[SiteRef, ...]


@dataclass(frozen=True, slots=True)
class SiteAccess:
    site_id: uuid.UUID
    name: str
    slug: str
    role: SiteRole
    permissions: frozenset[Permission]
    person_id: uuid.UUID | None
    is_derived: bool
    organization_id: uuid.UUID | None
    organization_role: OrganizationRole | None

    def can(self, permission: Permission | str) -> bool:
        return permission in self.permissions


@dataclass(frozen=True, slots=True)
class UserAccess:
    user_id: uuid.UUID
    full_name: str
    kind: UserKind
    is_platform_admin: bool
    sites: tuple[SiteAccess, ...] = field(default=())

    @property
    def can_see_portfolio(self) -> bool:
        return len(self.sites) > 1

    def site(self, site_id: uuid.UUID) -> SiteAccess | None:
        return next((s for s in self.sites if s.site_id == site_id), None)


def _site_role(value: str) -> SiteRole | None:
    try:
        return SiteRole(value)
    except ValueError:
        return None


def _organization_role(value: str) -> OrganizationRole | None:
    try:
        return OrganizationRole(value)
    except ValueError:
        return None


def resolve_access(
    *,
    user_id: uuid.UUID,
    full_name: str,
    kind: str,
    is_active: bool,
    is_platform_admin: bool,
    explicit: Iterable[ExplicitMembership],
    organizations: Iterable[OrganizationMembership],
) -> UserAccess:
    """Kullanıcının sitelerdeki erişimini tek seferde çözer (docs/05 §4).

    - Pasif kullanıcı hiçbir siteye erişemez.
    - Platform yöneticisinin site üyeliği yoktur; kayıtta olsa bile yok sayılır (§5).
    - Şirket üyeliği, şirketin bütün sitelerine eşlenmiş rolle erişim verir.
    - Açık site üyeliği türetilmişi ezer. **Pasif açık üyelik** erişim vermez ve o sitede
      türetilmiş erişimi de kapatır — bir sitedeki erişimi kaldırmanın güvenilir yolu.
    - Tanınmayan rol hiçbir izin vermez.
    """
    user_kind = UserKind(kind)
    base = UserAccess(user_id, full_name, user_kind, is_platform_admin)
    if not is_active or is_platform_admin:
        return base

    by_site: dict[uuid.UUID, SiteAccess] = {}
    for org in organizations:
        org_role = _organization_role(org.role)
        if not org.is_active or org_role is None:
            continue
        role = ORGANIZATION_ROLE_TO_SITE_ROLE[org_role]
        for site in org.sites:
            by_site[site.id] = SiteAccess(
                site_id=site.id,
                name=site.name,
                slug=site.slug,
                role=role,
                permissions=ROLE_PERMISSIONS[role],
                person_id=None,
                is_derived=True,
                organization_id=org.organization_id,
                organization_role=org_role,
            )

    for membership in explicit:
        site = membership.site
        site_role = _site_role(membership.role)
        if not membership.is_active or site_role is None:
            by_site.pop(site.id, None)
            continue
        by_site[site.id] = SiteAccess(
            site_id=site.id,
            name=site.name,
            slug=site.slug,
            role=site_role,
            permissions=ROLE_PERMISSIONS[site_role],
            person_id=membership.person_id,
            is_derived=False,
            organization_id=site.organization_id,
            organization_role=None,
        )

    sites = tuple(sorted(by_site.values(), key=lambda s: (s.name.casefold(), s.slug)))
    return UserAccess(user_id, full_name, user_kind, is_platform_admin, sites)
