"""Kimlik ve yetki — saf kurallar (docs/05-yetki.md).

İlke: kod **izne** bakar, rol adına bakmaz (`access.can("finance.charge.post")`). Rol, izinlerin
bir kümesidir. Hazır rol şablonlarının izinleri **tek yerde**, `ROLE_PERMISSIONS`
(`TEMPLATE_PERMISSIONS`) tablosundadır; siteye özel roller veridir (`site_roles`, P18).

Bir kullanıcının bir sitede birden çok rol ataması olabilir (kişi × rol × kapsam × süre); izinler
**birleşir**. Blok/kapı kapsamlı atamanın izinleri yalnız kapsamı uygulayan uçlarda geçerlidir
(`SCOPABLE_PERMISSIONS`) — kapsamı bilmeyen bir uç bu izni tesis geneli sanmasın.
"""

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
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
    # İletişim bilgisi (telefon, e-posta): maskeli görür, "göster" ile açar — açma denetim kaydına
    # yazılır (YAP-03 S1–S2). Yönetim Kurulu ve diğer roller iletişim alanını hiç almaz.
    PEOPLE_CONTACT = "people.contact"
    REQUESTS_READ = "requests.read"
    REQUESTS_CREATE = "requests.create"
    REQUESTS_ASSIGN = "requests.assign"
    # Talep ayarları: kategori, SLA, takvim, hazır yanıt, tarife, tadilat kuralları (SR 47–52)
    REQUESTS_SETTINGS = "requests.settings"
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
    # Onay mekanizması (Yazılım Dokümanı ONY-01; karar vermek atamaya bağlı, izin istemez)
    APPROVAL_RULES_READ = "approvals.rules.read"
    APPROVAL_RULES_MANAGE = "approvals.rules.manage"  # + tüm talepleri görür (gözetim)
    # Belge arşivi (Yazılım Dokümanı RAP-03; servis isteği 78)
    DOCUMENTS_READ = "documents.read"  # gizli dışındaki belgeler (sakin kendi ucunu kullanır)
    DOCUMENTS_MANAGE = "documents.manage"  # yükleme ve yeni sürüm
    DOCUMENTS_CONFIDENTIAL = "documents.confidential"  # gizli belgeler; her açılış kayda geçer
    TRANSPARENCY_MANAGE = "transparency.manage"  # şeffaflık raporu hazırlama (RAP-02)
    TRANSPARENCY_PUBLISH = "transparency.publish"  # onay kuralı yoksa doğrudan yayın


class SiteRole(StrEnum):
    """Site rol şablonları. Adlar Türkçe saklanır (docs/05 §3)."""

    MANAGER = "Yönetici"
    BOARD_MEMBER = "Yönetim Kurulu Üyesi"
    AUDITOR = "Denetçi"
    ACCOUNTING = "Muhasebe"
    SECURITY = "Güvenlik"
    TECHNICIAN = "Teknik Personel"
    # Ürün Dokümanı 5.1 rol listesinden (P18 ile eklendi)
    HR = "İK Sorumlusu"
    ATTENDANT = "Görevli"
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
            P.REQUESTS_SETTINGS,
            P.ANNOUNCEMENTS_READ, P.ANNOUNCEMENTS_PUBLISH, P.EXPENSES_READ, P.EXPENSES_MANAGE,
            P.SECURITY_VISITORS, P.SECURITY_PACKAGES, P.MODULES_MANAGE, P.MEMBERS_MANAGE,
            P.AUDIT_READ, P.SECURITY_INCIDENTS, P.MEETINGS_READ, P.MEETINGS_MANAGE,
            P.POLLS_MANAGE, P.CONTRACTS_MANAGE, P.INVENTORY_READ, P.INVENTORY_MANAGE,
            P.STAFF_MANAGE, P.APPROVAL_RULES_READ, P.APPROVAL_RULES_MANAGE, P.PEOPLE_CONTACT,
            P.DOCUMENTS_READ, P.DOCUMENTS_MANAGE, P.DOCUMENTS_CONFIDENTIAL,
            P.TRANSPARENCY_MANAGE, P.TRANSPARENCY_PUBLISH,
        }
    ),
    SiteRole.BOARD_MEMBER: frozenset(
        {
            P.FINANCE_READ, P.UNITS_READ, P.PEOPLE_READ, P.REQUESTS_READ, P.ANNOUNCEMENTS_READ,
            P.EXPENSES_READ, P.FINANCE_CASH_READ, P.FINANCE_REPORTS_READ, P.MEETINGS_READ,
            P.INVENTORY_READ, P.APPROVAL_RULES_READ, P.DOCUMENTS_READ,
        }
    ),
    # KMK m.41 — salt okunur finans, kişisel veri görmez
    SiteRole.AUDITOR: frozenset(
        {
            P.FINANCE_READ, P.EXPENSES_READ, P.AUDIT_READ, P.FINANCE_CASH_READ,
            P.FINANCE_REPORTS_READ, P.MEETINGS_READ, P.DOCUMENTS_READ, P.DOCUMENTS_CONFIDENTIAL,
        }
    ),
    SiteRole.ACCOUNTING: frozenset(
        {
            P.FINANCE_READ, P.FINANCE_CHARGE_POST, P.FINANCE_PAYMENT_RECORD,
            P.FINANCE_BUDGET_MANAGE, P.EXPENSES_READ, P.EXPENSES_MANAGE, P.FINANCE_CASH_READ,
            P.FINANCE_CASH_MANAGE, P.FINANCE_REPORTS_READ, P.UNITS_READ, P.PEOPLE_READ,
            P.CONTRACTS_MANAGE, P.INVENTORY_READ, P.DOCUMENTS_READ, P.TRANSPARENCY_MANAGE,
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
    # Ürün Dokümanı 5.2: personel kartı, vardiya, izin, puantaj; finans ve güvenlik kaydı yok
    SiteRole.HR: frozenset(
        {
            P.STAFF_MANAGE, P.PEOPLE_READ, P.ANNOUNCEMENTS_READ, P.REQUESTS_CREATE,
        }
    ),
    # Görevli / kapıcı: kargo, talep açma, duyuru (Ürün Dokümanı 5.2 "Kısıtlı: kargo")
    SiteRole.ATTENDANT: frozenset(
        {
            P.SECURITY_PACKAGES, P.REQUESTS_CREATE, P.ANNOUNCEMENTS_READ,
        }
    ),
    # + kendi dairesine ait okuma, person_id üzerinden (docs/05 §6)
    SiteRole.RESIDENT: frozenset({P.ANNOUNCEMENTS_READ, P.REQUESTS_CREATE}),
}  # fmt: skip
# Hazır rol şablonlarının izinleri — TEK YER. Yeni izin eklenince ilgili şablona buradan eklenir.
TEMPLATE_PERMISSIONS = ROLE_PERMISSIONS

# Blok/kapı kapsamlı atamada geçerli olan izinler: yalnız kapsamı uygulayan uçların izinleri
# (güvenlik: kargo ve ziyaretçi — kayıt ve daire arama blokla sınırlanır). Diğer izinler için
# tesis kapsamı gerekir.
SCOPABLE_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.SECURITY_VISITORS, Permission.SECURITY_PACKAGES}
)

# Şablon anahtarı (API'de `role_key`) ve varsayılan rol düzeyi (K20: 1 en üst). Düzey veridir —
# site kendi sırasını belirleyebilir (`site_roles`); burası yalnız varsayılandır:
# Yönetici > YK üyesi > Muhasebe = Denetçi > Teknik = Güvenlik.
TEMPLATE_KEYS: dict[SiteRole, str] = {
    SiteRole.MANAGER: "manager",
    SiteRole.BOARD_MEMBER: "board",
    SiteRole.AUDITOR: "auditor",
    SiteRole.ACCOUNTING: "accounting",
    SiteRole.HR: "hr",
    SiteRole.SECURITY: "security",
    SiteRole.TECHNICIAN: "technical",
    SiteRole.ATTENDANT: "attendant",
    SiteRole.RESIDENT: "resident",
}
LEVEL_MIN, LEVEL_MAX = 1, 9
DEFAULT_LEVELS: dict[SiteRole, int] = {
    SiteRole.MANAGER: 1,
    SiteRole.BOARD_MEMBER: 2,
    SiteRole.AUDITOR: 3,
    SiteRole.ACCOUNTING: 3,
    SiteRole.HR: 3,
    SiteRole.SECURITY: 4,
    SiteRole.TECHNICIAN: 4,
    SiteRole.ATTENDANT: 4,
    SiteRole.RESIDENT: LEVEL_MAX,
}

# Yönetim şirketi rolü → sitedeki karşılığı (docs/05 §4)
ORGANIZATION_ROLE_TO_SITE_ROLE: dict[OrganizationRole, SiteRole] = {
    OrganizationRole.OWNER: SiteRole.MANAGER,
    OrganizationRole.MANAGER: SiteRole.MANAGER,
    OrganizationRole.ACCOUNTING: SiteRole.ACCOUNTING,
    OrganizationRole.VIEWER: SiteRole.BOARD_MEMBER,
}


# --- Erişim çözümleme -------------------------------------------------------


class RoleScope(StrEnum):
    SITE = "site"  # tesis geneli
    BLOCK = "block"  # yalnız bir blok
    GATE = "gate"  # yalnız bir kapı (GUV-08) — kapı tanımları gelince uygulanır


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """Etkin bir rol ataması ya da şirket üyeliğinden türetilmiş rol."""

    key: str
    name: str
    level: int
    permissions: frozenset[Permission]
    scope_kind: RoleScope = RoleScope.SITE
    block_id: uuid.UUID | None = None
    gate_id: uuid.UUID | None = None
    ends_at: datetime | None = None
    assignment_id: uuid.UUID | None = None


def template_grant(role: SiteRole, *, level: int | None = None) -> RoleGrant:
    """Hazır şablondan tesis kapsamlı rol (şirket üyeliğinden türetilen erişim, testler)."""
    return RoleGrant(
        key=TEMPLATE_KEYS[role],
        name=role.value,
        level=DEFAULT_LEVELS[role] if level is None else level,
        permissions=TEMPLATE_PERMISSIONS[role],
    )


def sort_grants(grants: Iterable[RoleGrant]) -> tuple[RoleGrant, ...]:
    """Ana rol önce: düzey (1 en üst), sonra tesis kapsamı, sonra ad."""
    return tuple(
        sorted(grants, key=lambda g: (g.level, g.scope_kind != RoleScope.SITE, g.name.casefold()))
    )


def combine_permissions(
    grants: Iterable[RoleGrant],
) -> tuple[frozenset[Permission], dict[Permission, frozenset[uuid.UUID]]]:
    """İzin birleşimi (PLT-09: en geniş izin). (tesis geneli izinler, blokla sınırlı izinler).

    Blok kapsamlı atama yalnız `SCOPABLE_PERMISSIONS` için blok bazında izin verir; tesis
    genelinde zaten olan izin blokla sınırlanmaz. Kapı kapsamı, kapı tanımları gelene kadar izin
    vermez (kapının hangi bloklara bağlı olduğu henüz bilinmiyor).
    """
    grants = list(grants)
    site_wide: set[Permission] = set()
    for grant in grants:
        if grant.scope_kind is RoleScope.SITE:
            site_wide |= grant.permissions
    scoped: dict[Permission, set[uuid.UUID]] = {}
    for grant in grants:
        if grant.scope_kind is RoleScope.BLOCK and grant.block_id is not None:
            for permission in (grant.permissions & SCOPABLE_PERMISSIONS) - site_wide:
                scoped.setdefault(permission, set()).add(grant.block_id)
    return frozenset(site_wide), {p: frozenset(b) for p, b in scoped.items()}


@dataclass(frozen=True, slots=True)
class SiteRef:
    id: uuid.UUID
    name: str
    slug: str
    organization_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ExplicitMembership:
    """Açık site üyeliği ve şu an etkin rol atamaları (süresi dolan/başlamayan hariç)."""

    site: SiteRef
    grants: tuple[RoleGrant, ...]
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
    roles: tuple[RoleGrant, ...]  # ana rol önce
    permissions: frozenset[Permission]  # tesis geneli
    person_id: uuid.UUID | None
    is_derived: bool
    organization_id: uuid.UUID | None
    organization_role: OrganizationRole | None
    scoped: Mapping[Permission, frozenset[uuid.UUID]] = field(default_factory=dict)

    @property
    def role(self) -> str:
        """Ana rol adı (en üst düzey) — geriye uyum: tek `role` alanı."""
        return self.roles[0].name if self.roles else ""

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(g.name for g in self.roles))

    def can(self, permission: Permission | str) -> bool:
        """Tesis genelinde izin. Blokla sınırlı izin burada **sayılmaz** (`blocks_for`)."""
        return permission in self.permissions

    def blocks_for(self, permission: Permission) -> frozenset[uuid.UUID] | None:
        """Kapsamı uygulayan uçlar için: `None` → tesis geneli; küme → yalnız bu bloklar
        (boş küme → izin yok)."""
        if permission in self.permissions:
            return None
        return self.scoped.get(permission, frozenset())

    @property
    def usable_permissions(self) -> frozenset[Permission]:
        """Ekranın açılabileceği izinler: tesis geneli + blokla sınırlı."""
        return self.permissions | frozenset(self.scoped)


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


def _organization_role(value: str) -> OrganizationRole | None:
    try:
        return OrganizationRole(value)
    except ValueError:
        return None


# (site, şablon) → sitenin bu şablon için belirlediği düzey (yoksa None → varsayılan)
LevelLookup = Callable[[uuid.UUID, SiteRole], int | None]


def resolve_access(
    *,
    user_id: uuid.UUID,
    full_name: str,
    kind: str,
    is_active: bool,
    is_platform_admin: bool,
    explicit: Iterable[ExplicitMembership],
    organizations: Iterable[OrganizationMembership],
    site_level: LevelLookup | None = None,
) -> UserAccess:
    """Kullanıcının sitelerdeki erişimini tek seferde çözer (docs/05 §4, PLT-02).

    - Pasif kullanıcı hiçbir siteye erişemez.
    - Platform yöneticisinin site üyeliği yoktur; kayıtta olsa bile yok sayılır (§5).
    - Şirket üyeliği, şirketin bütün sitelerine eşlenmiş rolle erişim verir.
    - Açık site üyeliği türetilmişi ezer. **Pasif açık üyelik** erişim vermez ve o sitede
      türetilmiş erişimi de kapatır — bir sitedeki erişimi kaldırmanın güvenilir yolu.
    - Etkin rol ataması kalmamış açık üyelik de erişim vermez (süresi dolan atama, tanınmayan
      ya da arşivlenmiş rol).
    - Birden çok rolün izinleri birleşir.
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
            grant = template_grant(role, level=site_level(site.id, role) if site_level else None)
            by_site[site.id] = SiteAccess(
                site_id=site.id,
                name=site.name,
                slug=site.slug,
                roles=(grant,),
                permissions=grant.permissions,
                person_id=None,
                is_derived=True,
                organization_id=org.organization_id,
                organization_role=org_role,
            )

    for membership in explicit:
        site = membership.site
        if not membership.is_active or not membership.grants:
            by_site.pop(site.id, None)
            continue
        permissions, scoped = combine_permissions(membership.grants)
        by_site[site.id] = SiteAccess(
            site_id=site.id,
            name=site.name,
            slug=site.slug,
            roles=sort_grants(membership.grants),
            permissions=permissions,
            person_id=membership.person_id,
            is_derived=False,
            organization_id=site.organization_id,
            organization_role=None,
            scoped=scoped,
        )

    sites = tuple(sorted(by_site.values(), key=lambda s: (s.name.casefold(), s.slug)))
    return UserAccess(user_id, full_name, user_kind, is_platform_admin, sites)
