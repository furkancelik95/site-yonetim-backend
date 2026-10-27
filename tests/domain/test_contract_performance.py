"""Sözleşme performansı saf kuralları (Yazılım Dokümanı 15.3, SAT-06)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.contract_performance import (
    CutKind,
    EscalationInput,
    EscalationKind,
    EscalationPeriod,
    KpiInput,
    KpiSource,
    PenaltyInput,
    PenaltyRule,
    ScoreRow,
    annual_effect,
    capped,
    check_definitions,
    check_index,
    compute_penalty,
    escalated_amount,
    escalation_date,
    index_period,
    index_rate,
    kpi_score,
    monthly_amount,
    number_tr,
    percent,
    total_score,
)

D = Decimal


def row(key: str, actual: str | None, target: str = "85", weight: int = 100) -> ScoreRow:
    return ScoreRow(key, key.upper(), KpiSource.MANUAL, D(target), weight,
                    D(actual) if actual else None, None)  # fmt: skip


def test_kabul_senaryosu_s1_bin_lira_kesinti() -> None:
    """20.000 TL/ay, hedef 85, gerçekleşen 72, "%10'dan fazla altında → %5" → 1.000,00 TL."""
    rule = PenaltyRule("k", D(10), CutKind.PERCENT, D(5))
    result = compute_penalty([row("k", "72")], [rule], D("20000.00"))
    assert result is not None
    assert result.amount == D("1000.00")
    assert result.reasons == ["K: gerçekleşen 72, hedef 85 (sınır 76,5) → %5 kesinti"]
    # Sınırın üstü (76,5 ≥) tetiklemez
    assert compute_penalty([row("k", "76.5")], [rule], D("20000.00")) is None


def test_ceza_sabit_ve_ust_sinir() -> None:
    rules = [
        PenaltyRule("a", D(0), CutKind.FIXED, D("15000.00")),
        PenaltyRule("b", D(0), CutKind.PERCENT, D(50)),
    ]
    result = compute_penalty([row("a", "10"), row("b", "10")], rules, D("20000.00"))
    assert result is not None
    assert result.amount == D("20000.00")  # aylık bedeli aşamaz
    no_monthly = compute_penalty([row("a", "10"), row("b", "10")], rules, None)
    assert no_monthly is not None
    assert no_monthly.amount == D("15000.00")


def test_puan_ve_toplam() -> None:
    assert kpi_score(D(72), D(85)) == 85  # 84,7 → 85
    assert kpi_score(D(99), D(90)) == 100
    assert kpi_score(None, D(90)) is None
    assert total_score([row("a", "72", weight=60), row("b", "95", "90", 40)]) == 91
    assert total_score([row("a", None)]) is None
    assert total_score([]) is None
    assert percent(25, 31) == D(81)
    assert percent(1, 0) is None
    assert number_tr(D("100.00")) == "100"
    assert number_tr(D("76.50")) == "76,5"


def test_aylik_bedel() -> None:
    assert monthly_amount(D("20000.00"), "monthly") == D("20000.00")
    assert monthly_amount(D("100000.00"), "yearly") == D("8333.33")
    assert monthly_amount(D("1000.00"), "once") is None
    assert monthly_amount(None, "monthly") is None
    assert annual_effect(D("20000.00"), D("26000.00"), "monthly") == D("72000.00")
    assert annual_effect(D("100.00"), D("110.00"), "once") == D("0.00")


@pytest.mark.parametrize(
    ("today", "period", "expected"),
    [
        (date(2026, 6, 20), EscalationPeriod.YEARLY, date(2026, 7, 15)),
        (date(2026, 8, 10), EscalationPeriod.YEARLY, date(2026, 7, 15)),  # 30 gün geç görünür
        (date(2026, 8, 20), EscalationPeriod.YEARLY, date(2027, 7, 15)),
        (date(2026, 6, 20), EscalationPeriod.SEMIANNUAL, date(2026, 7, 15)),
        (date(2025, 9, 1), EscalationPeriod.SEMIANNUAL, date(2026, 1, 15)),
    ],
)
def test_artis_tarihi(today: date, period: EscalationPeriod, expected: date) -> None:
    assert escalation_date(date(2025, 7, 15), date(2028, 1, 1), period, today) == expected


def test_artis_bitisten_sonra_yok_ve_oranlar() -> None:
    assert escalation_date(date(2025, 7, 15), date(2026, 7, 1), EscalationPeriod.YEARLY,
                           date(2026, 6, 1)) is None  # fmt: skip
    assert index_period(date(2026, 1, 15)) == "2025-12"
    assert index_rate(EscalationKind.AVG, D("33.51"), D("28.00")) == D("30.76")
    assert index_rate(EscalationKind.UFE, D(1), D(2)) == D(2)
    assert index_rate(EscalationKind.AVG, None, D(2)) is None
    assert capped(D("33.5"), D(30)) == D(30)
    assert capped(D("33.5"), None) == D("33.5")
    assert escalated_amount(D("20000.00"), D("30")) == D("26000.00")
    assert escalated_amount(D("333.33"), D("12.5")) == D("375.00")


def test_tanim_dogrulama() -> None:
    kpis = [KpiInput("a", "Denetim", "inspection", D(85), 60, None),
            KpiInput("b", "SLA", "sla", D(90), 40, "elevator")]  # fmt: skip
    penalties = [PenaltyInput("a", D(10), "fixed", D("500.00"))]
    clean, errors = check_definitions(
        kpis, penalties, EscalationInput("avg", None, "semiannual", D(40)),
        has_amount=True, has_monthly_amount=False, request_categories={"elevator"},
    )  # fmt: skip
    assert errors == {}
    assert [k.request_category for k in clean.kpis] == [None, "elevator"]
    assert clean.escalation.kind is EscalationKind.AVG
    assert clean.escalation.period is EscalationPeriod.SEMIANNUAL
    _, errors = check_definitions(
        [KpiInput("a", "SLA", "sla", D("85.555"), 100, "yok")],
        [PenaltyInput("a", D(5), "x", D(1))],
        EscalationInput("bogus", None, "monthly", None),
        has_amount=True, has_monthly_amount=True, request_categories=set(),
    )  # fmt: skip
    assert errors == {
        "kpis.0.target": "Hedef 1–100 (%) olmalı.",
        "kpis.0.request_category": "Talep kategorisi bulunamadı.",
        "penalties.0.cut_kind": "Kesinti tipini seçin.",
        "escalation.kind": "Artış kuralı seçin.",
        "escalation.period": "Artış dönemini seçin.",
    }


def test_endeks_dogrulama() -> None:
    assert check_index("2026-06", D("33.5"), D(28), this_month="2026-06") == {}
    assert set(check_index("2026-6", None, D("1.234"), this_month="2026-06")) == {
        "period", "tufe", "ufe",
    }  # fmt: skip
