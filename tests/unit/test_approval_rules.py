"""Onay mekanizması saf kuralları — domain/approvals.py (Yazılım Dokümanı Bölüm 6)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.approvals import (
    Condition,
    DayBasis,
    Quorum,
    QuorumKind,
    RejectBehavior,
    StepOutcome,
    add_days,
    check_timing,
    coverage_gaps,
    quorum_unreachable,
    ranges_overlap,
    step_outcome,
)

D = Decimal


@pytest.mark.parametrize(
    ("quorum", "n", "required"),
    [
        (Quorum(QuorumKind.ANY), 5, 1),
        (Quorum(QuorumKind.ALL), 5, 5),
        (Quorum(QuorumKind.MAJORITY), 5, 3),
        (Quorum(QuorumKind.MAJORITY), 4, 3),
        (Quorum(QuorumKind.MAJORITY), 2, 2),
        (Quorum(QuorumKind.COUNT, 3), 4, 3),  # ONY-01 S2: 3/5 → talep eden çıkınca 3/4
        (Quorum(QuorumKind.ANY), 0, 1),  # onaycı yok: adım tıkalı
    ],
)
def test_gereken_onay_sayisi(quorum: Quorum, n: int, required: int) -> None:
    assert quorum.required(n) == required


def test_onay_etiketi() -> None:
    assert Quorum(QuorumKind.COUNT, 3).label(4) == "3/4"


@pytest.mark.parametrize(
    ("amount", "matches"),
    [
        (D("10000.00"), True),  # üst sınır dahil
        (D("10000.01"), False),
        (D("0.01"), True),
        (None, False),  # tutar koşullu kural tutarsız talebe uygulanmaz
    ],
)
def test_tutar_araligi_ust_sinir_dahil(amount: Decimal | None, matches: bool) -> None:
    assert Condition(None, D("10000.00")).matches(amount, None) is matches


def test_alt_sinir_haric_ve_kaynak() -> None:
    middle = Condition(D("10000.00"), D("100000.00"))
    assert not middle.matches(D("10000.00"), None)
    assert middle.matches(D("10000.01"), None)
    emergency = Condition(source="emergency")
    assert emergency.matches(None, "emergency")
    assert not emergency.matches(None, None)
    assert Condition().matches(None, None)


def test_ozgulluk() -> None:
    assert Condition().specificity == 0
    assert Condition(None, D(10)).specificity == 1
    assert Condition(source="emergency").specificity == 2
    assert Condition(None, D(10), "emergency").specificity == 3


def test_aralik_cakismasi_dokuman_ornegi() -> None:
    """ONY-01 S1: 0–10.000 ve 10.000–100.000 varken 5.000–15.000 eklenemez."""
    first = Condition(None, D("10000"))
    second = Condition(D("10000"), D("100000"))
    assert not ranges_overlap(first, second)  # bitişik: (…, 10.000] ve (10.000, …]
    new = Condition(D("5000"), D("15000"))
    assert ranges_overlap(first, new)
    assert ranges_overlap(second, new)
    assert not ranges_overlap(Condition(source="emergency"), first)  # farklı kaynak
    assert ranges_overlap(Condition(), first)  # koşulsuz kural her aralıkla çakışır


def test_bosluk_uyarisi() -> None:
    assert coverage_gaps([Condition(None, D("10000")), Condition(D("20000"), None)]) == [
        (D("10000"), D("20000"))
    ]
    assert coverage_gaps([Condition(None, D("10000"))]) == [(D("10000"), None)]
    assert coverage_gaps([Condition(D("500"), None)]) == [(None, D("500"))]
    assert coverage_gaps([Condition()]) == []
    assert coverage_gaps([]) == []
    full = [Condition(None, D("10000")), Condition(D("10000"), D("100000")), Condition(D("100000"))]
    assert coverage_gaps(full) == []


@pytest.mark.parametrize(
    ("approvals", "rejections", "required", "behavior", "outcome"),
    [
        (1, 0, 1, RejectBehavior.SINGLE_REJECTS, StepOutcome.APPROVED),
        (0, 1, 3, RejectBehavior.SINGLE_REJECTS, StepOutcome.REJECTED),  # ONY-02 S2
        (2, 1, 3, RejectBehavior.COUNTS, StepOutcome.PENDING),
        (3, 1, 3, RejectBehavior.COUNTS, StepOutcome.APPROVED),  # ONY-02 S1
        (0, 3, 3, RejectBehavior.COUNTS, StepOutcome.PENDING),  # otomatik ret yok
    ],
)
def test_adim_sonucu(
    approvals: int, rejections: int, required: int, behavior: RejectBehavior, outcome: StepOutcome
) -> None:
    assert (
        step_outcome(
            approvals=approvals, rejections=rejections, required=required, behavior=behavior
        )
        is outcome
    )


def test_ulasilamaz_onay() -> None:
    assert quorum_unreachable(approvals=1, rejections=2, eligible=4, required=3)
    assert not quorum_unreachable(approvals=1, rejections=1, eligible=4, required=3)


def test_is_gunu_ve_takvim_gunu() -> None:
    friday = date(2026, 10, 16)
    assert add_days(friday, 2, DayBasis.CALENDAR) == date(2026, 10, 18)
    assert add_days(friday, 2, DayBasis.BUSINESS) == date(2026, 10, 20)  # cumartesi/pazar atlanır
    assert add_days(friday, 5, DayBasis.BUSINESS) == date(2026, 10, 23)
    assert add_days(friday, 0, DayBasis.BUSINESS) == friday


def test_sure_ayarlari() -> None:
    assert check_timing(2, 5) == {}
    assert check_timing(0, 5) == {"remind_after_days": "Hatırlatma 1–30 gün olmalı."}
    assert check_timing(5, 5) == {
        "escalate_after_days": "Eskalasyon günü hatırlatma gününden sonra olmalı."
    }
