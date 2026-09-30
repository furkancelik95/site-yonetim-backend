"""Tahakkuk motoru — docs/07 §2 altın testleri (test sitesi §0) ve ek kenar durumları."""

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.charging.engine import (
    AccountData,
    Preview,
    PreviewCharge,
    WarningKind,
    build_preview,
)
from site_yonetim.domain.finance import AllocationKind, AreaBasis, Frequency, PayerRule, ScopeKind
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind, PartyRole
from tests.domain.charging_site import (
    BLOCK_A,
    TYPE_21,
    ChargingSite,
    aidat,
    asansor,
    build_site,
    cati,
    composite,
    isitma,
    item,
    rule,
    yonetim,
)


@pytest.fixture
def site() -> ChargingSite:
    return build_site()


def total_of(preview: Preview, name: str) -> Decimal:
    return next(t.distributed for t in preview.item_totals if t.name == name)


def charges_of(preview: Preview, unit_name: str) -> list[PreviewCharge]:
    return [c for c in preview.charges if c.unit_name == unit_name]


def kinds(preview: Preview) -> list[WarningKind]:
    return [w.kind for w in preview.warnings]


def assert_item_totals_match(preview: Preview) -> None:
    """§4.7 kontrolü: her kalem kendi tutarına kuruşu kuruşuna eşit."""
    for total in preview.item_totals:
        assert total.distributed == total.amount, total


# --- 07 §2 ------------------------------------------------------------------------


def test_2_1_esit_dagitimda_kalem_toplami_tutar(site: ChargingSite) -> None:
    preview = build_preview(site.input(aidat()))
    assert total_of(preview, "Kapıcı maaşı") == Decimal("20000.00")
    assert preview.unit_count == 24
    assert preview.warnings == []
    assert {c.amount for c in preview.charges} == {Decimal("833.33"), Decimal("833.34")}


def test_2_2_arsa_payi_toplami_korunur(site: ChargingSite) -> None:
    preview = build_preview(site.input(cati()))
    assert total_of(preview, "Çatı onarımı") == Decimal("180000.00")
    assert_item_totals_match(preview)


def test_2_3_metrekare_toplami_korunur(site: ChargingSite) -> None:
    preview = build_preview(site.input(isitma()))
    assert total_of(preview, "Isıtma sabit payı") == Decimal("50000.00")


def test_2_4_daire_tipi_agirligi_toplami_korunur(site: ChargingSite) -> None:
    preview = build_preview(site.input(yonetim()))
    assert total_of(preview, "Yönetim ücreti") == Decimal("8000.00")


def test_2_5_bilesik_kural_toplami_korur(site: ChargingSite) -> None:
    heating = item(
        "Merkezi ısıtma",
        "600000",
        composite((AllocationKind.BY_AREA, "30"), (AllocationKind.EQUAL, "70")),
    )
    preview = build_preview(site.input(heating))
    assert total_of(preview, "Merkezi ısıtma") == Decimal("50000.00")
    assert preview.warnings == []
    line = preview.charges[0].lines[0]
    assert line.explanation == "%30 metrekare + %70 eşit bileşimi"
    assert line.allocation_kind is AllocationKind.COMPOSITE


def test_2_6_cok_kalemli_projede_her_kalem_ayri_tutar(site: ChargingSite) -> None:
    preview = build_preview(site.input(aidat(), isitma(), cati(), yonetim()))
    assert {t.name: t.distributed for t in preview.item_totals} == {
        "Kapıcı maaşı": Decimal("20000.00"),
        "Isıtma sabit payı": Decimal("50000.00"),
        "Çatı onarımı": Decimal("180000.00"),
        "Yönetim ücreti": Decimal("8000.00"),
    }
    assert preview.total_amount == Decimal("258000.00")
    assert_item_totals_match(preview)


def test_2_7_asansor_yalniz_asansorlu_bloga(site: ChargingSite) -> None:
    preview = build_preview(site.input(asansor(frozenset({BLOCK_A}))))
    assert total_of(preview, "Asansör bakımı") == Decimal("3000.00")
    assert len(preview.charges) == 12
    assert all(c.unit_name.startswith("A-") for c in preview.charges)


def test_2_8_bos_kapsam_uyari_uretir_cokmez(site: ChargingSite) -> None:
    preview = build_preview(site.input(asansor(frozenset({uuid.uuid7()}))))
    assert kinds(preview) == [WarningKind.EMPTY_SCOPE]
    assert preview.charges == []


def test_2_9_aidat_kiraciya_demirbas_malike(site: ChargingSite) -> None:
    preview = build_preview(site.input(aidat(), cati()))
    occupant, owner = charges_of(preview, "A-2")
    assert occupant.account_kind is AccountKind.OCCUPANT
    assert occupant.person_name.endswith("KİRACI")
    assert occupant.reference_code == "A2-K"
    assert [line.description for line in occupant.lines] == ["Kapıcı maaşı"]
    assert owner.account_kind is AccountKind.OWNER
    assert owner.person_name.endswith("MALİK")
    assert owner.reference_code == "A2-M"
    assert [line.description for line in owner.lines] == ["Çatı onarımı"]


def test_2_10_kiraci_yoksa_aidat_malikin_oturan_hesabina(site: ChargingSite) -> None:
    [charge] = charges_of(build_preview(site.input(aidat())), "A-1")
    assert charge.account_kind is AccountKind.OCCUPANT
    assert charge.person_name.endswith("MALİK")
    assert charge.reference_code == "A1-O"


def test_2_11_kiraci_cikmissa_borc_malike(site: ChargingSite) -> None:
    site.end_parties("A-2", date(2026, 12, 31), PartyRole.TENANT)
    # malike oturan hesabı kiracı çıkınca açılır (docs/03 §5) — burada elle ekliyoruz
    owner = next(p for p in site.parties_of("A-2") if p.role is PartyRole.OWNER)
    site.accounts.append(
        AccountData(uuid.uuid7(), owner.unit_id, owner.person_id, AccountKind.OCCUPANT, "A2-O")
    )
    [charge] = charges_of(build_preview(site.input(aidat())), "A-2")
    assert charge.person_name.endswith("MALİK")
    assert charge.account_kind is AccountKind.OCCUPANT


def test_2_12_aktif_taraf_yoksa_uyari_kosu_durmaz(site: ChargingSite) -> None:
    site.end_parties("A-1", date(2026, 1, 1))
    preview = build_preview(site.input(aidat()))
    assert kinds(preview) == [WarningKind.NO_ACTIVE_PARTY]
    assert preview.warnings[0].unit_id == site.unit("A-1").id
    assert charges_of(preview, "A-1") == []
    assert preview.unit_count == 23


def test_2_13_her_satir_kendi_dayanagini_tasir(site: ChargingSite) -> None:
    preview = build_preview(site.input(isitma()))
    [charge] = charges_of(preview, "A-2")
    [line] = charge.lines
    assert line.allocation_kind is AllocationKind.BY_AREA
    assert line.source_amount == Decimal("50000.00")
    assert "m²" in line.explanation
    # A: 6×110 + 6×75 · B: 4×105 + 8×72 → 2.106 m²
    assert line.explanation == "Brüt 110,00 m² / 2.106,00 m² × 50.000,00 TL"
    assert (line.weight, line.weight_total) == (Decimal(110), Decimal(2106))


def test_2_14_sabit_tutar_her_daireye_ayni(site: ChargingSite) -> None:
    parking = item(
        "Otopark bedeli", "0", rule(AllocationKind.FIXED_PER_UNIT, fixed_amount=Decimal(250))
    )
    preview = build_preview(site.input(parking))
    assert {c.amount for c in preview.charges} == {Decimal("250.00")}
    assert preview.total_amount == Decimal("6000.00")
    assert preview.charges[0].lines[0].explanation == "Bağımsız bölüm başına sabit tutar"
    assert_item_totals_match(preview)


def test_2_15_arsa_payi_eksik_daire_uyari_digerleri_dagitilir(site: ChargingSite) -> None:
    site.replace_unit("A-1", land_share_numerator=None, land_share_denominator=None)
    preview = build_preview(site.input(cati()))
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA]
    assert "A-1" in preview.warnings[0].message
    assert total_of(preview, "Çatı onarımı") == Decimal("180000.00")
    assert charges_of(preview, "A-1") == []


def test_2_16_bilesik_yuzde_100_degilse_uyarir_ama_dagitir(site: ChargingSite) -> None:
    heating = item(
        "Merkezi ısıtma",
        "600000",
        composite((AllocationKind.BY_AREA, "30"), (AllocationKind.EQUAL, "50")),
    )
    preview = build_preview(site.input(heating))
    assert kinds(preview) == [WarningKind.COMPOSITE_PERCENT_MISMATCH]
    assert "80" in preview.warnings[0].message
    assert total_of(preview, "Merkezi ısıtma") == Decimal("50000.00")


def test_2_17_pasif_daireye_tahakkuk_kesilmez(site: ChargingSite) -> None:
    site.replace_unit("B-5", is_active=False)
    preview = build_preview(site.input(aidat()))
    assert preview.unit_count == 23
    assert total_of(preview, "Kapıcı maaşı") == Decimal("20000.00")
    assert charges_of(preview, "B-5") == []


# --- ek kenar durumları -------------------------------------------------------------


def test_explanations_for_each_kind(site: ChargingSite) -> None:
    custom = rule(
        AllocationKind.BY_CUSTOM_WEIGHT,
        unit_weights={u.id: Decimal(2) if u.name == "A-1" else Decimal(1) for u in site.units},
    )
    preview = build_preview(
        site.input(
            aidat(),
            cati(),
            yonetim(),
            item("Özel", "12000", custom),
            item("Net alan", "12000", rule(AllocationKind.BY_AREA, area_basis=AreaBasis.NET)),
        )
    )
    [occupant, owner] = charges_of(preview, "A-1")
    texts = {line.description: line.explanation for line in (*occupant.lines, *owner.lines)}
    assert texts["Kapıcı maaşı"] == "20.000,00 TL, 24 bağımsız bölüme eşit paylaştırıldı"
    assert texts["Çatı onarımı"] == "Arsa payı 0,033 / 0,884 × 180.000,00 TL"
    assert texts["Yönetim ücreti"] == "Daire tipi ağırlığı 1 / 27 × 8.000,00 TL"
    assert texts["Özel"] == "Özel ağırlık 2 / 25 × 1.000,00 TL"
    assert texts["Net alan"].startswith("Net 65,00 m² / ")


def test_meter_consumption(site: ChargingSite) -> None:
    water = item("Su", "12000", AllocationKind.BY_METER_CONSUMPTION)
    data = site.input(water)
    meter = {
        (water.id, site.unit("A-1").id): Decimal("12.5"),
        (water.id, site.unit("A-3").id): Decimal("37.5"),
    }
    preview = build_preview(replace(data, meter=meter))
    assert {c.unit_name: c.amount for c in preview.charges} == {
        "A-1": Decimal("250.00"),
        "A-3": Decimal("750.00"),
    }
    assert preview.charges[0].lines[0].explanation == "Sayaç tüketimi 12,50 / 50,00 × 1.000,00 TL"
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA]  # 22 bölümde okuma yok
    assert "…" in preview.warnings[0].message  # ilk 5'i adıyla


def test_weight_data_missing_everywhere_skips_item(site: ChargingSite) -> None:
    preview = build_preview(site.input(item("Su", "12000", AllocationKind.BY_METER_CONSUMPTION)))
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA]
    assert "hiçbir" in preview.warnings[0].message
    assert preview.charges == []


def test_composite_component_without_data_is_skipped_total_kept(site: ChargingSite) -> None:
    heating = item(
        "Merkezi ısıtma",
        "600000",
        composite((AllocationKind.BY_METER_CONSUMPTION, "70"), (AllocationKind.BY_AREA, "30")),
    )
    preview = build_preview(site.input(heating))
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA]
    assert total_of(preview, "Merkezi ısıtma") == Decimal("50000.00")  # tamamı m²'ye


def test_composite_without_usable_components(site: ChargingSite) -> None:
    empty = item("Boş", "1200", composite())
    only_meter = item("Sayaç", "1200", composite((AllocationKind.BY_METER_CONSUMPTION, "100")))
    preview = build_preview(site.input(empty, only_meter))
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA, WarningKind.MISSING_WEIGHT_DATA]
    assert preview.charges == []


def test_missing_rule_and_zero_fixed_amount(site: ChargingSite) -> None:
    no_rule = replace(aidat(), rule=None)
    zero_fixed = item("Sabit", "0", rule(AllocationKind.FIXED_PER_UNIT))
    preview = build_preview(site.input(no_rule, zero_fixed))
    assert kinds(preview) == [WarningKind.MISSING_WEIGHT_DATA, WarningKind.MISSING_WEIGHT_DATA]


def test_scope_by_unit_type_and_usage(site: ChargingSite) -> None:
    by_type = item(
        "Tip", "12000", AllocationKind.EQUAL, scope_kind=ScopeKind.UNIT_TYPES,
        scope_unit_type_ids=frozenset({TYPE_21}),
    )  # fmt: skip
    site.replace_unit("B-1", usage="commercial")
    by_usage = item(
        "Ticari", "1200", AllocationKind.EQUAL, scope_kind=ScopeKind.USAGE, scope_usage="commercial"
    )
    preview = build_preview(site.input(by_type, by_usage))
    typed = {c.unit_name for c in preview.charges if c.lines[0].description == "Tip"}
    assert typed == {f"A-{n}" for n in range(2, 13, 2)} | {"B-3", "B-6", "B-9", "B-12"}
    assert [c.unit_name for c in preview.charges if c.lines[-1].description == "Ticari"] == ["B-1"]


def test_owner_item_without_owner_warns(site: ChargingSite) -> None:
    site.end_parties("A-2", date(2026, 1, 1), PartyRole.OWNER)
    preview = build_preview(site.input(aidat(), cati()))
    assert kinds(preview) == [WarningKind.NO_ACTIVE_PARTY]
    assert "malik yok" in preview.warnings[0].message
    assert [c.account_kind for c in charges_of(preview, "A-2")] == [AccountKind.OCCUPANT]


def test_missing_or_closed_account_warns(site: ChargingSite) -> None:
    unit = site.unit("A-1")
    site.accounts = [
        replace(a, is_closed=True) if a.unit_id == unit.id and a.kind is AccountKind.OCCUPANT else a
        for a in site.accounts
    ]
    preview = build_preview(site.input(aidat()))
    assert kinds(preview) == [WarningKind.MISSING_LEDGER_ACCOUNT]
    assert preview.unit_count == 23


def test_co_owners_bigger_share_pays(site: ChargingSite) -> None:
    unit = site.unit("A-1")
    first = next(p for p in site.parties_of("A-1") if p.role is PartyRole.OWNER)
    second_id = uuid.uuid7()
    site.parties = [
        replace(p, share_percent=Decimal(40)) if p is first else p for p in site.parties
    ]
    site.parties.append(
        replace(first, person_id=second_id, person_name="A-1 ORTAK", share_percent=Decimal(60))
    )
    site.accounts.append(AccountData(uuid.uuid7(), unit.id, second_id, AccountKind.OWNER, "A1-M2"))
    [charge] = charges_of(build_preview(site.input(cati())), "A-1")
    assert charge.person_name == "A-1 ORTAK"


@pytest.mark.parametrize(
    ("frequency", "amount"),
    [
        (Frequency.MONTHLY, "1000.00"),
        (Frequency.QUARTERLY, "3000.00"),
        (Frequency.YEARLY, "12000.00"),
        (Frequency.ONE_TIME, "12000.00"),
    ],
)
def test_period_amount_by_frequency(site: ChargingSite, frequency: Frequency, amount: str) -> None:
    preview = build_preview(
        site.input(item("K", "12000", AllocationKind.EQUAL, frequency=frequency))
    )
    assert preview.item_totals[0].amount == Decimal(amount)


def test_monthly_amount_is_rounded_each_month(site: ChargingSite) -> None:
    """K3 şimdiki davranış: round(1000 / 12) = 83,33 (yılda 4 kuruş fark)."""
    preview = build_preview(site.input(item("K", "1000", AllocationKind.EQUAL)))
    assert preview.item_totals[0].amount == Decimal("83.33")


def test_negative_amount_mirrors(site: ChargingSite) -> None:
    refund = item("İade", "-240000", AllocationKind.EQUAL)
    preview = build_preview(site.input(refund))
    assert total_of(preview, "İade") == Decimal("-20000.00")
    assert all(c.amount < ZERO for c in preview.charges)


def test_payer_rule_default_is_occupant() -> None:
    assert item("X", "1", AllocationKind.EQUAL).payer_rule is PayerRule.OCCUPANT
