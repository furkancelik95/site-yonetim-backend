"""Rol modeli — saf kurallar (Yazılım Dokümanı PLT-09, ONY-03, §6.4; PM kararları P18, K20).

- **Hazır şablonlar** koddadır (`domain/access.py` `TEMPLATE_PERMISSIONS`); site onları salt okur,
  yalnız **düzeyini** (K20 rol sırası) değiştirebilir.
- **Siteye özel rol**: bir şablonun (ya da başka bir özel rolün) kopyası; adı, açıklaması, izin
  seti ve düzeyi sitenindir. Ad, onay kurallarında rolü tanıttığı için sonradan değiştirilmez.
- Rol düzeyi 1 en üsttür. Yetki devri "aynı veya üst rol" (ONY-03), eskalasyon önerisi bu
  sıradan gelir.
- Atayan, kendi sahip olmadığı izni içeren rolü atayamaz, tanımlayamaz, kaldıramaz (PLT-09).
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from site_yonetim.domain.access import (
    DEFAULT_LEVELS,
    LEVEL_MAX,
    LEVEL_MIN,
    TEMPLATE_KEYS,
    TEMPLATE_PERMISSIONS,
    Permission,
    RoleGrant,
    RoleScope,
    SiteRole,
)

# --- Hazır şablonlar -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    key: str
    role: SiteRole
    description: str


TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate("manager", SiteRole.MANAGER, "Bütün işlemler; kullanıcı ve modül yönetimi"),
    RoleTemplate("board", SiteRole.BOARD_MEMBER, "Finans, gider ve talepleri okur; değiştirmez"),
    RoleTemplate("auditor", SiteRole.AUDITOR, "Finansı ve denetim kaydını okur; kişisel veri yok"),
    RoleTemplate("accounting", SiteRole.ACCOUNTING, "Tahakkuk, tahsilat, gider, kasa ve raporlar"),
    RoleTemplate("hr", SiteRole.HR, "Personel kartları, vardiya, izin ve puantaj; finans görmez"),
    RoleTemplate("security", SiteRole.SECURITY, "Ziyaretçi, kargo ve olay kayıtları; borç görmez"),
    RoleTemplate("technical", SiteRole.TECHNICIAN, "Kendisine atanan talepleri yürütür"),
    RoleTemplate("attendant", SiteRole.ATTENDANT, "Kargo kabulü ve talep açma (görevli / kapıcı)"),
    RoleTemplate("resident", SiteRole.RESIDENT, "Sakin: kendi bölümü, duyurular, talepler"),
)  # fmt: skip
TEMPLATE_BY_KEY = {t.key: t for t in TEMPLATES}
TEMPLATE_BY_NAME = {t.role.value: t for t in TEMPLATES}
RESIDENT_KEY = TEMPLATE_KEYS[SiteRole.RESIDENT]


class RoleKind(StrEnum):
    SYSTEM = "system"  # hazır şablon (salt okunur; düzeyi site belirler)
    CUSTOM = "custom"  # siteye özel


@dataclass(frozen=True, slots=True)
class RoleDef:
    """Sitede kullanılabilir bir rol (şablon + site düzeyi ya da özel rol)."""

    key: str
    name: str
    description: str
    level: int
    permissions: frozenset[Permission]
    kind: RoleKind
    base_key: str | None
    is_active: bool = True
    id: uuid.UUID | None = None  # özel rolde (ya da düzeyi değiştirilmiş şablonda) satır kimliği

    @property
    def is_staff(self) -> bool:
        return self.key != RESIDENT_KEY

    def grant(
        self,
        *,
        scope_kind: RoleScope = RoleScope.SITE,
        block_id: uuid.UUID | None = None,
        gate_id: uuid.UUID | None = None,
        ends_at: datetime | None = None,
        assignment_id: uuid.UUID | None = None,
    ) -> RoleGrant:
        return RoleGrant(
            key=self.key,
            name=self.name,
            level=self.level,
            permissions=self.permissions,
            scope_kind=scope_kind,
            block_id=block_id,
            gate_id=gate_id,
            ends_at=ends_at,
            assignment_id=assignment_id,
        )


@dataclass(frozen=True, slots=True)
class StoredRole:
    """`site_roles` satırının saf karşılığı (servis doldurur)."""

    id: uuid.UUID
    key: str
    name: str
    description: str | None
    base_key: str | None
    is_system: bool
    permissions: tuple[str, ...] | None
    level: int
    is_active: bool


def parse_permissions(values: Iterable[str]) -> frozenset[Permission]:
    """Saklı izin adları → `Permission`; artık tanınmayan ad yok sayılır (izin vermez)."""
    known = {p.value for p in Permission}
    return frozenset(Permission(v) for v in values if v in known)


@dataclass(frozen=True, slots=True)
class RoleCatalog:
    roles: tuple[RoleDef, ...]  # düzey, ad sırasıyla

    def by_key(self, key: str) -> RoleDef | None:
        return next((r for r in self.roles if r.key == key), None)

    def by_name(self, name: str) -> RoleDef | None:
        folded = name.casefold()
        return next((r for r in self.roles if r.name.casefold() == folded), None)

    @property
    def staff(self) -> tuple[RoleDef, ...]:
        """Personele atanabilir etkin roller (sakin hariç)."""
        return tuple(r for r in self.roles if r.is_staff and r.is_active)

    def level_of(self, name: str) -> int | None:
        role = self.by_name(name)
        return role.level if role else None


def build_catalog(stored: Iterable[StoredRole]) -> RoleCatalog:
    """Şablonlar + sitenin şablon düzeyleri + özel roller."""
    rows = list(stored)
    system_levels = {r.key: r for r in rows if r.is_system}
    roles: list[RoleDef] = []
    for template in TEMPLATES:
        override = system_levels.get(template.key)
        roles.append(
            RoleDef(
                key=template.key,
                name=template.role.value,
                description=template.description,
                level=override.level if override else DEFAULT_LEVELS[template.role],
                permissions=TEMPLATE_PERMISSIONS[template.role],
                kind=RoleKind.SYSTEM,
                base_key=None,
                id=override.id if override else None,
            )
        )
    for row in rows:
        if row.is_system:
            continue
        roles.append(
            RoleDef(
                key=row.key,
                name=row.name,
                description=row.description or "",
                level=row.level,
                permissions=parse_permissions(row.permissions or ()),
                kind=RoleKind.CUSTOM,
                base_key=row.base_key,
                is_active=row.is_active,
                id=row.id,
            )
        )
    roles.sort(key=lambda r: (r.level, r.kind is RoleKind.CUSTOM, r.name.casefold()))
    return RoleCatalog(tuple(roles))


# --- İzin kataloğu (izin ağacı: modül → izin, hassasiyet) ---------------------------


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"  # hassas: kişisel veri ya da para hareketi
    CRITICAL = "critical"  # kritik: atanması onaydan geçer (PLT-09 — P16b)


GROUP_LABELS: dict[str, str] = {
    "finance": "Finans",
    "expenses": "Giderler",
    "units": "Yapı ve bölümler",
    "people": "Kişiler",
    "staff": "İnsan kaynakları",
    "requests": "Talepler",
    "announcements": "İletişim",
    "polls": "İletişim",
    "security": "Güvenlik",
    "meetings": "Toplantı ve kararlar",
    "contracts": "Satın alma ve sözleşmeler",
    "inventory": "Demirbaş ve stok",
    "documents": "Belgeler",
    "approvals": "Onaylar",
    "members": "Ayarlar",
    "modules": "Ayarlar",
    "audit": "Ayarlar",
    "portfolio": "Portföy",
}
OTHER_GROUP = "Diğer"

DESCRIPTIONS: dict[str, str] = {
    "finance.read": "Finans panosu, borçlular, cari ekstre, işletme projesi",
    "finance.charge.post": "Tahakkuk kesme ve ters kaydetme",
    "finance.payment.record": "Tahsilat kaydetme",
    "finance.budget.manage": "İşletme projesi yönetimi",
    "finance.cash.read": "Kasa ve banka görüntüleme",
    "finance.cash.manage": "Kasa ve banka hareketi, aktarım",
    "finance.reports.read": "Finans raporları ve Excel",
    "units.read": "Bölümleri görüntüleme",
    "units.manage": "Bölüm yönetimi ve Excel aktarımı",
    "people.read": "Kişileri ve personel listesini görüntüleme",
    "people.manage": "Kişi yönetimi",
    "requests.read": "Talepleri görüntüleme",
    "requests.create": "Talep açma",
    "requests.assign": "Talep atama ve durum değiştirme",
    "announcements.read": "Duyuruları görüntüleme",
    "announcements.publish": "Duyuru yayınlama",
    "expenses.read": "Giderleri görüntüleme",
    "expenses.manage": "Gider kaydı, ödeme ve geri alma",
    "security.visitors": "Ziyaretçi kaydı",
    "security.packages": "Kargo kaydı ve teslimi",
    "security.incidents": "Olay kaydı ve kayıp eşya",
    "modules.manage": "Modül aç/kapa",
    "members.manage": "Kullanıcı ve rol yönetimi",
    "portfolio.read": "Portföy görüntüleme",
    "audit.read": "Denetim kaydını görüntüleme",
    "meetings.read": "Toplantıları görüntüleme",
    "meetings.manage": "Toplantı planlama, karar, iptal",
    "polls.manage": "Anket açma ve kapatma",
    "contracts.manage": "Hizmet sözleşmesi yönetimi",
    "inventory.read": "Demirbaş ve stok görüntüleme",
    "inventory.manage": "Demirbaş ve stok yönetimi, stok çıkışı",
    "staff.manage": "Personel kartı, vardiya, izin, puantaj",
    "approvals.rules.read": "Onay kurallarını görüntüleme",
    "approvals.rules.manage": "Onay kurallarını yönetme, tüm onay taleplerini görme",
    "documents.read": "Belge arşivi (gizli olmayan)",
    "documents.manage": "Belge yükleme ve yeni sürüm",
    "documents.confidential": "Gizli belgeler (her açılış kayda geçer)",
}

# PLT-09: "finans iadesi, rol yönetimi, dönem kilidi" — atanması onaydan geçer (P16b).
CRITICAL: frozenset[str] = frozenset(
    {"members.manage", "approvals.rules.manage", "finance.charge.post", "modules.manage"}
)
SENSITIVE: frozenset[str] = frozenset(
    {
        "finance.payment.record", "finance.cash.manage", "finance.budget.manage",
        "expenses.manage", "people.read", "people.manage", "staff.manage", "audit.read",
        "documents.confidential",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    key: str
    group: str
    description: str
    sensitivity: Sensitivity


def permission_info(permission: Permission | str) -> PermissionInfo:
    """Yeni eklenen ve henüz kataloğa yazılmamış izin "Diğer" grubunda, adıyla görünür."""
    key = str(permission)
    prefix = key.split(".", 1)[0]
    sensitivity = (
        Sensitivity.CRITICAL
        if key in CRITICAL
        else Sensitivity.SENSITIVE
        if key in SENSITIVE
        else Sensitivity.NORMAL
    )
    return PermissionInfo(
        key, GROUP_LABELS.get(prefix, OTHER_GROUP), DESCRIPTIONS.get(key, key), sensitivity
    )


def permission_groups() -> list[tuple[str, list[PermissionInfo]]]:
    """İzin ağacı: grup → izinler (grup sırası `GROUP_LABELS`, sonra "Diğer")."""
    order = list(dict.fromkeys([*GROUP_LABELS.values(), OTHER_GROUP]))
    groups: dict[str, list[PermissionInfo]] = {name: [] for name in order}
    for permission in Permission:
        info = permission_info(permission)
        groups[info.group].append(info)
    return [(name, items) for name, items in groups.items() if items]


def is_critical(permissions: Iterable[Permission | str]) -> bool:
    return any(str(p) in CRITICAL for p in permissions)


# --- Kurallar -------------------------------------------------------------------------

NAME_MIN, NAME_MAX = 3, 60
DESCRIPTION_MAX = 200


class RoleRuleError(Exception):
    """İş kuralı ihlali: `status` 403/404/409, `code` makine için, mesaj Türkçe."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


EXCEEDS_ASSIGN = "Kendi yetkinizden fazla izin içeren rol atayamazsınız."
EXCEEDS_DEFINE = "Kendi yetkinizden fazla izin içeren rol tanımlayamazsınız."
EXCEEDS_REMOVE = "Kendi yetkinizden fazla izin içeren rolü kaldıramazsınız."


def exceeding(actor: frozenset[Permission], role: Iterable[Permission]) -> frozenset[Permission]:
    """Atayanda olmayan izinler (PLT-09 adım 2) — boşsa atayabilir."""
    return frozenset(role) - actor


def check_role_fields(
    *,
    name: str | None,
    description: str | None,
    permissions: Iterable[str] | None,
    level: int | None,
    catalog: RoleCatalog,
    renaming: bool = True,
) -> tuple[str, str, frozenset[Permission], int, dict[str, str]]:
    """Özel rol alanları. (ad, açıklama, izinler, düzey, hatalar)."""
    errors: dict[str, str] = {}
    clean_name = " ".join((name or "").split())
    if renaming:
        if not NAME_MIN <= len(clean_name) <= NAME_MAX:
            errors["name"] = f"Rol adı {NAME_MIN}–{NAME_MAX} karakter olmalı."
        elif catalog.by_name(clean_name) is not None:
            errors["name"] = "Bu adla bir rol zaten var; farklı bir ad yazın."
    clean_description = " ".join((description or "").split())[:DESCRIPTION_MAX]
    raw = list(permissions or [])
    known = {p.value for p in Permission}
    unknown = [p for p in raw if p not in known]
    if unknown:
        errors["permissions"] = f"Tanınmayan izin: {', '.join(sorted(unknown)[:3])}."
    elif not raw:
        errors["permissions"] = "En az bir izin seçin."
    if level is None or not LEVEL_MIN <= level <= LEVEL_MAX:
        errors["level"] = f"Rol düzeyi {LEVEL_MIN} (en üst) ile {LEVEL_MAX} arasında olmalı."
    return (
        clean_name,
        clean_description,
        parse_permissions(raw),
        level or LEVEL_MAX,
        errors,
    )


def is_active_at(starts_at: datetime | None, ends_at: datetime | None, now: datetime) -> bool:
    """Atama bu anda etkin mi: başlangıç ≤ şimdi < bitiş (boş sınır = sınırsız)."""
    return (starts_at is None or starts_at <= now) and (ends_at is None or now < ends_at)


def periods_overlap(
    a_start: datetime | None,
    a_end: datetime | None,
    b_start: datetime | None,
    b_end: datetime | None,
) -> bool:
    """[başlangıç, bitiş) aralıkları kesişiyor mu (boş = sınırsız)."""
    a_before_b_end = b_end is None or a_start is None or a_start < b_end
    b_before_a_end = a_end is None or b_start is None or b_start < a_end
    return a_before_b_end and b_before_a_end


# --- Rol sırası: devir ve eskalasyon (K20) ---------------------------------------------


def can_take_over(role_level: int, target_levels: Iterable[int]) -> bool:
    """ONY-03 adım 2: devralan aynı ya da üst (düzeyi küçük ya da eşit) bir role sahip mi?"""
    return any(level <= role_level for level in target_levels)


def suggest_escalation(
    step_roles: Iterable[str], levels: Mapping[str, int], *, with_holders: Iterable[str] = ()
) -> str | None:
    """Eskalasyon hedefi önerisi (K20): adımlardaki en üst rolün hemen üstündeki düzeyden bir rol.

    Aynı düzeyde birden çok rol varsa önce aktif kullanıcısı olan, sonra ada göre ilki. Üstte
    rol yoksa öneri yok (yalnız hatırlatma gider).
    """
    step_levels = [levels[r] for r in step_roles if r in levels]
    if not step_levels:
        return None
    top = min(step_levels)
    above = [(lvl, name) for name, lvl in levels.items() if lvl < top]
    if not above:
        return None
    nearest = max(lvl for lvl, _ in above)
    holders = set(with_holders)
    candidates = sorted(
        (name for lvl, name in above if lvl == nearest),
        key=lambda n: (n not in holders, n.casefold()),
    )
    return candidates[0]
