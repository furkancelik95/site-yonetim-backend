"""Yetki kuralları — docs/05-yetki.md §2–§6."""

import uuid

import pytest

from site_yonetim.domain.access import (
    DEFAULT_LEVELS,
    ORGANIZATION_ROLE_TO_SITE_ROLE,
    ROLE_PERMISSIONS,
    ExplicitMembership,
    OrganizationMembership,
    OrganizationRole,
    Permission,
    SiteRef,
    SiteRole,
    UserKind,
    resolve_access,
    template_grant,
)

P = Permission
AKSU = SiteRef(uuid.uuid4(), "Aksu Konakları", "aksu-konaklari", None)
YILDIZ = SiteRef(uuid.uuid4(), "Yıldız Sitesi", "yildiz-sitesi", None)
ORG = uuid.uuid4()
KENT_A = SiteRef(uuid.uuid4(), "Kent A", "kent-a", ORG)
KENT_B = SiteRef(uuid.uuid4(), "Kent B", "kent-b", ORG)


def _m(
    site: SiteRef, role: str, is_active: bool = True, person_id: uuid.UUID | None = None
) -> ExplicitMembership:
    """Tek rollü açık üyelik (tanınmayan rol → etkin atama yok)."""
    grants = (template_grant(SiteRole(role)),) if role in set(SiteRole) else ()
    return ExplicitMembership(site, grants, is_active, person_id)


def _resolve(
    explicit: list[ExplicitMembership] | None = None,
    orgs: list[OrganizationMembership] | None = None,
    *,
    is_active: bool = True,
    is_platform_admin: bool = False,
) -> object:
    return resolve_access(
        user_id=uuid.uuid4(),
        full_name="Ayşe YILMAZ",
        kind="staff",
        is_active=is_active,
        is_platform_admin=is_platform_admin,
        explicit=explicit or [],
        organizations=orgs or [],
    )


def test_her_rolun_izin_kumesi_tanimli_ve_izinler_gecerli() -> None:
    assert set(ROLE_PERMISSIONS) == set(SiteRole)
    assert all(perms <= set(Permission) for perms in ROLE_PERMISSIONS.values())


def test_yonetici_portfoy_disinda_her_seyi_yapar() -> None:
    # support.view_as_resident hiçbir şablonda varsayılan değildir (PLT-12)
    assert ROLE_PERMISSIONS[SiteRole.MANAGER] == set(Permission) - {
        P.PORTFOLIO_READ,
        P.SUPPORT_VIEW_AS_RESIDENT,
    }


@pytest.mark.parametrize(
    "forbidden",
    [P.FINANCE_READ, P.PEOPLE_READ, P.UNITS_READ, P.EXPENSES_READ, P.FINANCE_CASH_READ],
)
def test_guvenlik_borc_kisi_muhasebe_goremez(forbidden: Permission) -> None:
    assert forbidden not in ROLE_PERMISSIONS[SiteRole.SECURITY]


def test_denetci_salt_okunur_finans_kisisel_veri_yok() -> None:
    perms = ROLE_PERMISSIONS[SiteRole.AUDITOR]

    assert P.FINANCE_READ in perms
    assert P.FINANCE_CASH_READ in perms
    assert P.PEOPLE_READ not in perms
    assert not {p for p in perms if p.value.endswith((".manage", ".post", ".record"))}


def test_yonetim_kurulu_gorur_degistirmez() -> None:
    perms = ROLE_PERMISSIONS[SiteRole.BOARD_MEMBER]

    assert all(p.value.endswith(".read") for p in perms)


def test_sakin_yalniz_duyuru_okur_talep_acar() -> None:
    assert ROLE_PERMISSIONS[SiteRole.RESIDENT] == {P.ANNOUNCEMENTS_READ, P.REQUESTS_CREATE}


def test_sirket_rolu_eslemesi() -> None:
    assert ORGANIZATION_ROLE_TO_SITE_ROLE == {
        OrganizationRole.OWNER: SiteRole.MANAGER,
        OrganizationRole.MANAGER: SiteRole.MANAGER,
        OrganizationRole.ACCOUNTING: SiteRole.ACCOUNTING,
        OrganizationRole.VIEWER: SiteRole.BOARD_MEMBER,
    }


def test_ayni_kisi_iki_sitede_farkli_rol() -> None:
    access = _resolve(
        [
            _m(AKSU, "Yönetici", is_active=True),
            _m(YILDIZ, "Güvenlik", is_active=True),
        ]
    )

    aksu, yildiz = access.site(AKSU.id), access.site(YILDIZ.id)  # type: ignore[attr-defined]
    assert aksu.role == SiteRole.MANAGER
    assert aksu.can("finance.read")
    assert yildiz.role == SiteRole.SECURITY
    assert not yildiz.can("finance.read")
    assert access.can_see_portfolio  # type: ignore[attr-defined]


def test_sirket_uyeligi_butun_sitelere_turetilmis_erisim() -> None:
    access = _resolve(orgs=[OrganizationMembership(ORG, "Muhasebe", True, (KENT_A, KENT_B))])

    sites = access.sites  # type: ignore[attr-defined]
    assert {s.slug for s in sites} == {"kent-a", "kent-b"}
    assert all(s.is_derived and s.role == SiteRole.ACCOUNTING for s in sites)
    assert sites[0].organization_role is OrganizationRole.ACCOUNTING


def test_acik_uyelik_turetilmisi_ezer() -> None:
    # Şirket muhasebecisini tek bir sitede Denetçi'ye daraltmak (docs/05 §4)
    access = _resolve(
        [_m(KENT_A, "Denetçi", is_active=True)],
        [OrganizationMembership(ORG, "Muhasebe", True, (KENT_A, KENT_B))],
    )

    assert access.site(KENT_A.id).role == SiteRole.AUDITOR  # type: ignore[attr-defined]
    assert not access.site(KENT_A.id).is_derived  # type: ignore[attr-defined]
    assert access.site(KENT_B.id).role == SiteRole.ACCOUNTING  # type: ignore[attr-defined]


def test_pasif_acik_uyelik_erisim_vermez_ve_turetilmisi_de_kapatir() -> None:
    access = _resolve(
        [_m(KENT_A, "Yönetici", is_active=False)],
        [OrganizationMembership(ORG, "Sahip", True, (KENT_A, KENT_B))],
    )

    assert access.site(KENT_A.id) is None  # type: ignore[attr-defined]
    assert access.site(KENT_B.id) is not None  # type: ignore[attr-defined]


def test_pasif_sirket_uyeligi_erisim_vermez() -> None:
    access = _resolve(orgs=[OrganizationMembership(ORG, "Sahip", False, (KENT_A,))])

    assert access.sites == ()  # type: ignore[attr-defined]


def test_pasif_kullanici_hicbir_siteye_erisemez() -> None:
    access = _resolve([_m(AKSU, "Yönetici", is_active=True)], is_active=False)

    assert access.sites == ()  # type: ignore[attr-defined]


def test_platform_yoneticisinin_site_uyeligi_yok_sayilir() -> None:
    access = _resolve([_m(AKSU, "Yönetici", is_active=True)], is_platform_admin=True)

    assert access.is_platform_admin  # type: ignore[attr-defined]
    assert access.sites == ()  # type: ignore[attr-defined]


def test_taninmayan_rol_izin_vermez() -> None:
    access = _resolve([_m(AKSU, "Süper Kullanıcı", is_active=True)])

    assert access.site(AKSU.id) is None  # type: ignore[attr-defined]


def test_tek_siteli_kullanici_portfoy_gormez_ve_tur_korunur() -> None:
    access = resolve_access(
        user_id=uuid.uuid4(),
        full_name="Sakin",
        kind="resident",
        is_active=True,
        is_platform_admin=False,
        explicit=[_m(AKSU, "Sakin", True, person_id=uuid.uuid4())],
        organizations=[],
    )

    assert not access.can_see_portfolio
    assert access.kind is UserKind.RESIDENT
    assert access.sites[0].person_id is not None


def test_taninmayan_sirket_rolu_erisim_vermez() -> None:
    access = _resolve(orgs=[OrganizationMembership(ORG, "Kral", True, (KENT_A,))])

    assert access.sites == ()  # type: ignore[attr-defined]


def test_servis_istekleri_09_18_izinleri() -> None:
    """Karar: Furkan, 05.10.2026 (docs/05 §2–3)."""
    roles = ROLE_PERMISSIONS
    assert P.SECURITY_INCIDENTS in roles[SiteRole.SECURITY]
    assert P.SECURITY_INCIDENTS not in roles[SiteRole.AUDITOR]  # olayda kişisel veri olabilir
    assert {P.INVENTORY_READ, P.INVENTORY_MANAGE} <= roles[SiteRole.TECHNICIAN]  # stok çıkışı
    assert P.CONTRACTS_MANAGE in roles[SiteRole.ACCOUNTING]
    assert P.INVENTORY_MANAGE not in roles[SiteRole.ACCOUNTING]
    assert P.MEETINGS_READ in roles[SiteRole.AUDITOR]
    assert P.MEETINGS_MANAGE not in roles[SiteRole.BOARD_MEMBER]
    only_manager = {P.MEETINGS_MANAGE, P.POLLS_MANAGE, P.STAFF_MANAGE}
    for role, perms in roles.items():
        if role is SiteRole.HR:  # İK sorumlusu personel işini yürütür (Ürün Dokümanı 5.2)
            assert perms & only_manager == {P.STAFF_MANAGE}
        elif role is not SiteRole.MANAGER:
            assert not (perms & only_manager), role


def test_varsayilan_rol_sirasi_k20() -> None:
    """Yönetici > YK üyesi > Muhasebe = Denetçi > Teknik = Güvenlik (PM kararı K20)."""
    level = DEFAULT_LEVELS
    assert level[SiteRole.MANAGER] < level[SiteRole.BOARD_MEMBER] < level[SiteRole.ACCOUNTING]
    assert level[SiteRole.ACCOUNTING] == level[SiteRole.AUDITOR]
    assert level[SiteRole.ACCOUNTING] < level[SiteRole.TECHNICIAN] == level[SiteRole.SECURITY]
