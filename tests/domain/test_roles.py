"""Rol modeli — saf kurallar (PLT-09, ONY-03, PM kararları P18, K20)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from site_yonetim.domain.access import (
    TEMPLATE_KEYS,
    TEMPLATE_PERMISSIONS,
    ExplicitMembership,
    Permission,
    RoleScope,
    SiteRef,
    SiteRole,
    combine_permissions,
    resolve_access,
    template_grant,
)
from site_yonetim.domain.roles import (
    TEMPLATE_BY_KEY,
    RoleKind,
    Sensitivity,
    StoredRole,
    build_catalog,
    can_take_over,
    check_role_fields,
    exceeding,
    is_active_at,
    periods_overlap,
    permission_groups,
    permission_info,
    suggest_escalation,
)

P = Permission
AKSU = SiteRef(uuid.uuid4(), "Aksu Konakları", "aksu-konaklari", None)
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
BLOCK_A, BLOCK_B = uuid.uuid4(), uuid.uuid4()


def _custom(
    name: str, permissions: tuple[str, ...], level: int = 3, *, active: bool = True
) -> StoredRole:
    return StoredRole(
        uuid.uuid4(), f"custom-{name[:4]}", name, None, "accounting", False, permissions, level,
        active,
    )  # fmt: skip


def test_her_sablonun_anahtari_ve_izinleri_var() -> None:
    assert set(TEMPLATE_KEYS) == set(SiteRole) == set(TEMPLATE_PERMISSIONS)
    assert set(TEMPLATE_BY_KEY) == set(TEMPLATE_KEYS.values())


def test_izinler_birlesir_en_genis_izin() -> None:
    access = resolve_access(
        user_id=uuid.uuid4(), full_name="Ayşe", kind="staff", is_active=True,
        is_platform_admin=False, organizations=[],
        explicit=[
            ExplicitMembership(
                AKSU,
                (template_grant(SiteRole.SECURITY), template_grant(SiteRole.ACCOUNTING)),
                True,
            )
        ],
    )  # fmt: skip
    site = access.sites[0]
    assert site.can(P.SECURITY_VISITORS)
    assert site.can(P.FINANCE_CASH_MANAGE)
    assert site.role == "Muhasebe"  # ana rol: en üst düzey
    assert site.role_names == ("Muhasebe", "Güvenlik")


def test_blok_kapsamli_atama_yalniz_kapsamli_izin_verir() -> None:
    security = template_grant(SiteRole.SECURITY)
    scoped = template_grant(SiteRole.MANAGER)
    grants = [
        replace(security, scope_kind=RoleScope.BLOCK, block_id=BLOCK_A),
        replace(scoped, scope_kind=RoleScope.BLOCK, block_id=BLOCK_B),
    ]
    site_wide, by_block = combine_permissions(grants)
    assert site_wide == frozenset()  # blok kapsamı tesis geneli izin vermez (finans vb. yok)
    assert by_block[P.SECURITY_VISITORS] == {BLOCK_A, BLOCK_B}
    assert P.FINANCE_READ not in by_block


def test_tesis_geneli_izin_blokla_sinirlanmaz() -> None:
    security = template_grant(SiteRole.SECURITY)
    block = replace(security, scope_kind=RoleScope.BLOCK, block_id=BLOCK_A)
    site_wide, by_block = combine_permissions([security, block])
    assert P.SECURITY_VISITORS in site_wide
    assert by_block == {}


def test_atamasiz_acik_uyelik_erisim_vermez() -> None:
    access = resolve_access(
        user_id=uuid.uuid4(), full_name="Ayşe", kind="staff", is_active=True,
        is_platform_admin=False, organizations=[],
        explicit=[ExplicitMembership(AKSU, (), True)],
    )  # fmt: skip
    assert access.sites == ()


def test_katalog_sablon_duzeyi_ve_ozel_rol() -> None:
    level_override = StoredRole(
        uuid.uuid4(), "security", "Güvenlik", None, None, True, None, 2, True
    )
    custom = _custom("Kasa Sorumlusu", ("finance.cash.read", "finance.cash.manage", "eski.izin"))
    catalog = build_catalog([level_override, custom])
    assert catalog.by_key("security").level == 2  # type: ignore[union-attr]
    role = catalog.by_name("kasa sorumlusu")
    assert role is not None
    assert role.kind is RoleKind.CUSTOM
    assert role.permissions == {P.FINANCE_CASH_READ, P.FINANCE_CASH_MANAGE}  # tanınmayan atlanır
    assert "resident" not in {r.key for r in catalog.staff}


def test_ozel_rol_alanlari() -> None:
    catalog = build_catalog([])
    *_, errors = check_role_fields(
        name="Yönetici", description=None, permissions=["yok.boyle"], level=12, catalog=catalog
    )
    assert set(errors) == {"name", "permissions", "level"}
    name, _, perms, level, errors = check_role_fields(
        name="  Kasa   Sorumlusu ", description="", permissions=["finance.cash.read"], level=3,
        catalog=catalog,
    )  # fmt: skip
    assert (name, level, errors) == ("Kasa Sorumlusu", 3, {})
    assert perms == {P.FINANCE_CASH_READ}


def test_kendi_izninden_fazlasi() -> None:
    accounting = TEMPLATE_PERMISSIONS[SiteRole.ACCOUNTING]
    assert exceeding(accounting, {P.FINANCE_READ}) == frozenset()
    assert exceeding(accounting, {P.FINANCE_READ, P.MEMBERS_MANAGE}) == {P.MEMBERS_MANAGE}


def test_atama_suresi() -> None:
    hour = timedelta(hours=1)
    assert is_active_at(None, None, NOW)
    assert not is_active_at(NOW + hour, None, NOW)  # ileri tarihli
    assert not is_active_at(None, NOW, NOW)  # bitiş anında biter
    assert is_active_at(NOW - hour, NOW + hour, NOW)
    assert periods_overlap(None, None, NOW, None)
    assert not periods_overlap(None, NOW, NOW, None)


def test_devir_ayni_ya_da_ust_rol() -> None:
    # Muhasebe (3) yetkisini YK üyesi (2) ya da başka muhasebeci (3) alabilir; güvenlik (4) alamaz
    assert can_take_over(3, [2])
    assert can_take_over(3, [3])
    assert not can_take_over(3, [4])
    assert can_take_over(3, [4, 1])


def test_eskalasyon_onerisi_rol_sirasindan() -> None:
    levels = {"Yönetici": 1, "Yönetim Kurulu Üyesi": 2, "Muhasebe": 3, "Denetçi": 3, "Güvenlik": 4}
    assert suggest_escalation(["Muhasebe"], levels) == "Yönetim Kurulu Üyesi"
    assert suggest_escalation(["Güvenlik"], levels) in {"Muhasebe", "Denetçi"}
    assert suggest_escalation(["Güvenlik"], levels, with_holders=["Muhasebe"]) == "Muhasebe"
    assert suggest_escalation(["Yönetici"], levels) is None
    assert suggest_escalation(["Muhasebe", "Yönetim Kurulu Üyesi"], levels) == "Yönetici"
    assert suggest_escalation(["Bilinmeyen"], levels) is None


def test_izin_agaci() -> None:
    groups = dict(permission_groups())
    assert {i.key for i in groups["Finans"]} >= {"finance.read", "finance.charge.post"}
    assert sum(len(items) for items in groups.values()) == len(Permission)
    assert permission_info(P.MEMBERS_MANAGE).sensitivity is Sensitivity.CRITICAL
    assert permission_info("yeni.izin").group == "Diğer"
