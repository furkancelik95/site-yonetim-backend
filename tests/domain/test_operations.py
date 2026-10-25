"""Talep durum akışı (K18) ve duyuru hedef kitlesi — saf (docs/03 §9, HIZ-02 "Durumlar")."""

import uuid
from datetime import date

import pytest

from site_yonetim.domain.operations import (
    Audience,
    Channel,
    OperationRuleError,
    PartyRef,
    RequestEventKind,
    RequestState,
    RequestStatus,
    channels_of,
    check_status_change,
    next_statuses,
    recipients,
    reopen_status,
)
from site_yonetim.domain.structure import PartyRole

TODAY = date(2026, 10, 1)
BLOCK_A, BLOCK_B = uuid.uuid7(), uuid.uuid7()
S = RequestStatus


def party(role: PartyRole, block: uuid.UUID = BLOCK_A, *, ended: bool = False) -> PartyRef:
    return PartyRef(
        uuid.uuid7(),
        uuid.uuid7(),
        block,
        role,
        date(2020, 1, 1),
        date(2026, 1, 1) if ended else None,
    )


def state(status: RequestStatus, *, assignee: bool = True, resident: bool = True) -> RequestState:
    return RequestState(status, has_assignee=assignee, awaits_resident=resident)


# --- talep --------------------------------------------------------------------------


def test_status_change_description_and_event() -> None:
    change = check_status_change(state(S.ASSIGNED), S.IN_PROGRESS, None)
    assert change.event is RequestEventKind.STATUS_CHANGED
    assert change.description == "Durum: Atandı → İşlemde"
    assert (change.resolved, change.finished) == (False, False)


def test_document_flow_is_allowed() -> None:
    """Açık → Atandı → İşlemde → Beklemede → İşlemde → Çözüldü."""
    for current, new in [
        (S.OPEN, S.ASSIGNED),
        (S.ASSIGNED, S.IN_PROGRESS),
        (S.IN_PROGRESS, S.WAITING),
        (S.WAITING, S.IN_PROGRESS),
        (S.WAITING, S.RESOLVED),
    ]:
        check_status_change(state(current), new, "Yapıldı")


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.OPEN, S.IN_PROGRESS),  # önce atanır
        (S.OPEN, S.RESOLVED),
        (S.ASSIGNED, S.RESOLVED),
        (S.IN_PROGRESS, S.OPEN),
        (S.RESOLVED, S.OPEN),
    ],
)
def test_transitions_outside_table_are_rejected(current: RequestStatus, new: RequestStatus) -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(state(current), new, "x")
    assert (caught.value.code, caught.value.conflict) == ("invalid_transition", True)


@pytest.mark.parametrize("final", [S.CLOSED, S.CANCELLED])
def test_closed_and_cancelled_cannot_be_reopened(final: RequestStatus) -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(state(final), S.OPEN, None)
    assert caught.value.code == "request_finished"


@pytest.mark.parametrize("status", [S.RESOLVED, S.CANCELLED])
def test_reason_required_when_resolving_or_cancelling(status: RequestStatus) -> None:
    for empty in (None, "", "   "):
        with pytest.raises(OperationRuleError) as caught:
            check_status_change(state(S.IN_PROGRESS), status, empty)
        assert caught.value.field == "resolution"
    change = check_status_change(state(S.IN_PROGRESS), status, "  Conta   değişti ")
    assert change.description.endswith(": Conta değişti")


def test_resolved_and_cancelled_event_kinds() -> None:
    resolved = check_status_change(state(S.IN_PROGRESS), S.RESOLVED, "Tamam")
    assert (resolved.event, resolved.resolved, resolved.finished) == (
        RequestEventKind.RESOLVED, True, False,
    )  # fmt: skip
    cancelled = check_status_change(state(S.OPEN), S.CANCELLED, "Yanlış açıldı")
    assert (cancelled.event, cancelled.finished) == (RequestEventKind.CANCELLED, True)


def test_same_status_is_rejected() -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(state(S.OPEN), S.OPEN, None)
    assert caught.value.conflict is True


def test_assigned_needs_assignee() -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(state(S.OPEN, assignee=False), S.ASSIGNED, None)
    assert caught.value.code == "assignee_required"


def test_staff_closes_only_when_no_resident_waits() -> None:
    with pytest.raises(OperationRuleError) as caught:
        check_status_change(state(S.RESOLVED), S.CLOSED, None)
    assert caught.value.code == "awaiting_confirmation"
    change = check_status_change(state(S.RESOLVED, resident=False), S.CLOSED, None)
    assert (change.event, change.finished) == (RequestEventKind.CLOSED, True)


def test_next_statuses() -> None:
    assert next_statuses(state(S.OPEN, assignee=False)) == [S.CANCELLED]
    assert next_statuses(state(S.OPEN)) == [S.ASSIGNED, S.CANCELLED]
    assert next_statuses(state(S.RESOLVED)) == [S.IN_PROGRESS]
    assert next_statuses(state(S.RESOLVED, resident=False)) == [S.IN_PROGRESS, S.CLOSED]
    assert next_statuses(state(S.CLOSED)) == []


def test_reopen_status() -> None:
    assert reopen_status(True) is S.ASSIGNED
    assert reopen_status(False) is S.OPEN


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
