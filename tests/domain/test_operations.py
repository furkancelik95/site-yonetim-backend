"""Talep durum kuralı ve duyuru hedef kitlesi — saf (docs/03 §9)."""

import uuid
from datetime import date

import pytest

from site_yonetim.domain.operations import (
    Audience,
    Channel,
    OperationRuleError,
    PartyRef,
    RequestEventKind,
    RequestStatus,
    channels_of,
    check_status_change,
    recipients,
)
from site_yonetim.domain.structure import PartyRole

TODAY = date(2026, 10, 1)
BLOCK_A, BLOCK_B = uuid.uuid7(), uuid.uuid7()


def party(role: PartyRole, block: uuid.UUID = BLOCK_A, *, ended: bool = False) -> PartyRef:
    return PartyRef(
        uuid.uuid7(),
        uuid.uuid7(),
        block,
        role,
        date(2020, 1, 1),
        date(2026, 1, 1) if ended else None,
    )


# --- talep --------------------------------------------------------------------------


def test_status_change_description_and_event() -> None:
    change = check_status_change(RequestStatus.OPEN, RequestStatus.IN_PROGRESS, None)
    assert change.event is RequestEventKind.STATUS_CHANGED
    assert change.description == "Durum: Açık → İşlemde"
    assert change.resolved is False


@pytest.mark.parametrize("status", [RequestStatus.RESOLVED, RequestStatus.CLOSED])
def test_resolution_required_when_resolving_or_closing(status: RequestStatus) -> None:
    for empty in (None, "", "   "):
        with pytest.raises(OperationRuleError) as caught:
            check_status_change(RequestStatus.IN_PROGRESS, status, empty)
        assert caught.value.field == "resolution"
    change = check_status_change(RequestStatus.IN_PROGRESS, status, "  Conta   değişti ")
    assert change.resolved is True
    assert change.description.endswith("Çözüm: Conta değişti")


def test_resolved_event_kind() -> None:
    change = check_status_change(RequestStatus.OPEN, RequestStatus.RESOLVED, "Tamam")
    assert change.event is RequestEventKind.RESOLVED


def test_same_status_is_rejected() -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(RequestStatus.OPEN, RequestStatus.OPEN, None)
    assert caught.value.conflict is True


def test_reopening_is_allowed() -> None:
    change = check_status_change(RequestStatus.CLOSED, RequestStatus.OPEN, None)
    assert change.resolved is False


# --- duyuru -------------------------------------------------------------------------


def test_audiences() -> None:
    owner_a = party(PartyRole.OWNER)
    tenant_a = party(PartyRole.TENANT)
    resident_b = party(PartyRole.RESIDENT, BLOCK_B)
    proxy = party(PartyRole.PROXY)
    former = party(PartyRole.TENANT, ended=True)
    everyone = [owner_a, tenant_a, resident_b, proxy, former]

    def ids(*parties: PartyRef) -> set[uuid.UUID]:
        return {p.person_id for p in parties}

    assert recipients(everyone, audience=Audience.ALL_RESIDENTS, today=TODAY) == ids(
        owner_a, tenant_a, resident_b
    )
    assert recipients(everyone, audience=Audience.BLOCKS, today=TODAY, block_ids={BLOCK_B}) == ids(
        resident_b
    )
    assert recipients(everyone, audience=Audience.OWNERS_ONLY, today=TODAY) == ids(owner_a)
    assert recipients(everyone, audience=Audience.TENANTS_ONLY, today=TODAY) == ids(tenant_a)
    debtors = {tenant_a.person_id, former.person_id, proxy.person_id}
    assert recipients(
        everyone, audience=Audience.DEBTORS_ONLY, today=TODAY, debtor_person_ids=debtors
    ) == ids(tenant_a)


def test_in_app_channel_always_first() -> None:
    assert channels_of([Channel.SMS, Channel.EMAIL, Channel.SMS]) == [
        Channel.IN_APP,
        Channel.SMS,
        Channel.EMAIL,
    ]
    assert channels_of([]) == [Channel.IN_APP]
