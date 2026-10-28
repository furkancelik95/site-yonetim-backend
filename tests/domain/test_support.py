"""Platform destek saf kuralları — PYN-04, PYN-06, PLT-12 (servis istekleri 82–84)."""

from datetime import UTC, datetime, timedelta

import pytest

from site_yonetim.domain.access import (
    Permission,
    SiteRef,
    SupportGrant,
    resolve_access,
)
from site_yonetim.domain.roles import exceeding
from site_yonetim.domain.support import (
    SUPPORT_SESSION_PERMISSIONS,
    SupportSessionStatus,
    SupportTier,
    TicketPriority,
    TicketRuleError,
    TicketStatus,
    ViewAsStatus,
    after_customer_message,
    after_support_message,
    clean_screens,
    close_by_customer,
    first_breached,
    resolve_breached,
    routes_to_on_call,
    screen_matches,
    session_effective_status,
    sla_due,
    ticket_number,
    view_as_effective_status,
)

T0 = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


def test_sla_varsayilan_hedefler() -> None:
    due = sla_due(SupportTier.BUSINESS_HOURS, TicketPriority.CRITICAL, T0)
    assert (due.first_due, due.resolve_due) == (T0 + timedelta(hours=1), T0 + timedelta(hours=8))
    due = sla_due(SupportTier.FULL_24_7, TicketPriority.NORMAL, T0)
    assert (due.first_due, due.resolve_due) == (T0 + timedelta(hours=8), T0 + timedelta(hours=72))


def test_sla_asimi() -> None:
    due = T0 + timedelta(hours=1)
    assert first_breached(due, None, T0 + timedelta(hours=2))
    assert not first_breached(due, T0 + timedelta(minutes=30), T0 + timedelta(hours=5))
    assert resolve_breached(due, T0 + timedelta(hours=2), T0)


@pytest.mark.parametrize(
    ("tier", "priority", "expected"),
    [
        (SupportTier.BUSINESS_HOURS, TicketPriority.CRITICAL, False),
        (SupportTier.CRITICAL_24_7, TicketPriority.CRITICAL, True),
        (SupportTier.FULL_24_7, TicketPriority.CRITICAL, True),
        (SupportTier.FULL_24_7, TicketPriority.HIGH, False),
    ],
)
def test_nobet_hatti_yalniz_7_24_pakette(
    tier: SupportTier, priority: TicketPriority, expected: bool
) -> None:
    assert routes_to_on_call(tier, priority) is expected


def test_bilet_durum_gecisleri() -> None:
    assert after_customer_message(TicketStatus.RESOLVED) is TicketStatus.OPEN
    assert after_support_message(TicketStatus.OPEN, TicketStatus.RESOLVED) is TicketStatus.RESOLVED
    assert close_by_customer(TicketStatus.RESOLVED) is TicketStatus.CLOSED
    with pytest.raises(TicketRuleError) as closed:
        after_customer_message(TicketStatus.CLOSED)
    assert closed.value.code == "closed"
    with pytest.raises(TicketRuleError) as early:
        close_by_customer(TicketStatus.ANSWERED)
    assert early.value.code == "invalid_status"
    assert ticket_number(7) == "DST-0007"


def test_ekran_eslesmesi() -> None:
    assert clean_screens(["/satin-alma/", "satin-alma", " ", "rezervasyon"]) == [
        "satin-alma",
        "rezervasyon",
    ]
    assert screen_matches(["satin-alma"], "/satin-alma/talepler")
    assert not screen_matches(["satin-alma"], "satin-almalar")


def test_suresi_dolan_oturum_sona_ermis_sayilir() -> None:
    assert (
        session_effective_status(SupportSessionStatus.ACTIVE, T0, T0)
        is SupportSessionStatus.EXPIRED
    )
    assert (
        session_effective_status(SupportSessionStatus.ACTIVE, T0 + timedelta(hours=1), T0)
        is SupportSessionStatus.ACTIVE
    )
    assert view_as_effective_status(ViewAsStatus.ACTIVE, T0, T0) is ViewAsStatus.EXPIRED


def test_destek_oturumu_salt_okunur_erisim() -> None:
    import uuid

    site = SiteRef(uuid.uuid4(), "Aksu", "aksu", None)
    grant = SupportGrant(site, uuid.uuid4(), T0, SUPPORT_SESSION_PERMISSIONS)
    access = resolve_access(
        user_id=uuid.uuid4(),
        full_name="Pelin",
        kind="staff",
        is_active=True,
        is_platform_admin=True,
        explicit=(),
        organizations=(),
        support=[grant],
    )
    found = access.site(site.id)
    assert found is not None
    assert found.read_only
    assert not any(p.value.endswith((".manage", ".post", ".record")) for p in found.permissions)
    assert Permission.PEOPLE_CONTACT not in found.permissions
    # Pasif platform kullanıcısı: hiçbir şey
    passive = resolve_access(
        user_id=uuid.uuid4(),
        full_name="Pelin",
        kind="staff",
        is_active=False,
        is_platform_admin=True,
        explicit=(),
        organizations=(),
        support=[grant],
    )
    assert passive.sites == ()


def test_sakin_olarak_goruntule_iznini_yonetici_verir() -> None:
    manager = frozenset({Permission.MEMBERS_MANAGE, Permission.PEOPLE_READ})
    assert exceeding(manager, [Permission.SUPPORT_VIEW_AS_RESIDENT]) == frozenset()
    other = frozenset({Permission.PEOPLE_READ})
    assert exceeding(other, [Permission.SUPPORT_VIEW_AS_RESIDENT]) == frozenset(
        {Permission.SUPPORT_VIEW_AS_RESIDENT}
    )
