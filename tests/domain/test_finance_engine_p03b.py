"""Finans motoru, ikinci yarı (P03b) — saf kurallar: K9 gün bazlı kıst, aidat kuralları (FIN-02),
gecikme hesabı (FIN-03, docs/04 §6, docs/07 §3.11–3.12), taslak anlık görüntüsü (FIN-05)."""

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.charging import snapshot
from site_yonetim.domain.charging.billing_rules import (
    VersionFields,
    check_version,
    scopes_overlap,
    to_item,
)
from site_yonetim.domain.charging.engine import (
    AccountData,
    ChargeInput,
    ComponentData,
    Exemption,
    ItemSource,
    UnitData,
    WarningKind,
    build_preview,
    simulate,
)
from site_yonetim.domain.charging.payments import (
    LateFeePolicyData,
    LateFeeRule,
    accrued_late_fee,
    late_fee,
)
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import AllocationKind, PayerRule, ScopeKind
from site_yonetim.domain.finance_params import Proration
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind, PartyRole
from tests.domain.charging_site import BLOCK_A, TYPE_11, TYPE_21, ChargingSite, build_site, item


@pytest.fixture
def site() -> ChargingSite:
    return build_site()


# --- K9 gün bazlı kıst (FIN-05 S3) -----------------------------------------------------------


def _owner_occupant_account(site: ChargingSite, name: str) -> None:
    """Kiracı çıkınca malikin oturan hesabı (gerçekte ilişki kapanırken açılır)."""
    unit = site.unit(name)
    owner = next(p for p in site.parties_of(name) if p.role is PartyRole.OWNER)
    site.accounts.append(
        AccountData(uuid.uuid7(), unit.id, owner.person_id, AccountKind.OCCUPANT, "A2-O")
    )


def _october(site: ChargingSite, **changes: object) -> ChargeInput:
    return replace(
        site.input(item("Aidat", "12400", AllocationKind.EQUAL)),
        charge_date=date(2026, 10, 1),
        due_date=date(2026, 10, 10),
        **changes,  # type: ignore[arg-type]
    )


def test_k9_kiraci_15_ekimde_cikinca_15_31_ve_16_31(site: ChargingSite) -> None:
    """FIN-05 S3: 15 Ekim'de çıkan kiracı → kiracıya 15/31, malike (yeni sorumlu) 16/31."""
    site.end_parties("A-2", date(2026, 10, 15), PartyRole.TENANT)
    _owner_occupant_account(site, "A-2")
    data = replace(_october(site), units=[site.unit("A-2")])
    preview = build_preview(data)
    by_person = {c.person_name: c for c in preview.charges}
    tenant, owner = by_person["A-2 KİRACI"], by_person["A-2 MALİK"]
    # 12.400 / 12 = 1.033,33 (Ekim payı, D-5 dağıtımı); 15/31 + 16/31
    assert tenant.amount + owner.amount == preview.item_totals[0].amount
    assert (tenant.days, owner.days, tenant.period_days) == (15, 16, 31)
    assert tenant.amount == Decimal("500.00")
    assert owner.amount == Decimal("533.33")
    assert tenant.account_kind is owner.account_kind is AccountKind.OCCUPANT
    assert "kıst 15/31 gün" in tenant.lines[0].explanation
    assert preview.warnings == []


def test_k9_ay_bazli_parametrede_tahakkuk_gunu_kim_varsa_tamami(site: ChargingSite) -> None:
    site.end_parties("A-2", date(2026, 10, 15), PartyRole.TENANT)
    data = replace(_october(site, proration=Proration.MONTH), units=[site.unit("A-2")])
    [charge] = build_preview(data).charges
    assert charge.person_name == "A-2 KİRACI"
    assert charge.days is None


def test_k9_kimsenin_olmadigi_gunler_yazilmaz_uyari_uretir(site: ChargingSite) -> None:
    """Malik 11 Ekim'de başlıyor: 1–10 Ekim için etkin kimse yok → o pay yazılmaz, uyarı."""
    unit = site.unit("A-1")
    site.parties = [
        replace(p, start_date=date(2026, 10, 11)) if p.unit_id == unit.id else p
        for p in site.parties
    ]
    preview = build_preview(replace(_october(site), units=[unit]))
    [charge] = preview.charges
    assert charge.days == 21
    assert [w.kind for w in preview.warnings] == [WarningKind.NO_ACTIVE_PARTY]
    assert "01.10.2026–10.10.2026" in preview.warnings[0].message


def test_k9_tum_satirlar_ayri_ayri_kuruşu_korur(site: ChargingSite) -> None:
    site.end_parties("A-2", date(2026, 10, 7), PartyRole.TENANT)
    _owner_occupant_account(site, "A-2")
    data = replace(
        site.input(
            item("Aidat", "1000", AllocationKind.EQUAL),
            item("Yakıt", "777.77", AllocationKind.EQUAL),
        ),
        charge_date=date(2026, 10, 1),
        units=[site.unit("A-2")],
    )
    preview = build_preview(data)
    assert sum((c.amount for c in preview.charges), ZERO) == sum(
        (t.amount for t in preview.item_totals), ZERO
    )


# --- Aidat kuralı (FIN-02) ---------------------------------------------------------------------


def _three_units() -> list[UnitData]:
    shares = (33, 33, 34)
    return [
        UnitData(
            id=uuid.uuid7(),
            name=f"A-{i + 1}",
            block_id=BLOCK_A,
            land_share_numerator=n,
            land_share_denominator=100,
        )
        for i, n in enumerate(shares)
    ]


def _fields(
    kind: AllocationKind, amount: str, note: str = "Yönetim planı md. 12", **changes: object
) -> VersionFields:
    return VersionFields(
        effective=YearMonth(2026, 11),
        kind=kind,
        amount=Decimal(amount),
        basis_note=note,
        **changes,  # type: ignore[arg-type]
    )


def _trial(units: list[UnitData], fields: VersionFields, **kw: object) -> list[Decimal]:
    rule_item = to_item(
        uuid.uuid7(), "Aidat", fields, payer_rule=PayerRule.OCCUPANT, owner_item=False
    )
    data = ChargeInput(date(2026, 11, 1), date(2026, 11, 10), [rule_item], units, [], [])
    return [line.amount for _, line in simulate(data, rule_item).lines]


def test_fin02_s1_arsa_payi_330_330_340() -> None:
    assert _trial(_three_units(), _fields(AllocationKind.BY_LAND_SHARE, "1000")) == [
        Decimal("330.00"),
        Decimal("330.00"),
        Decimal("340.00"),
    ]


def test_fin02_s2_esit_333_34_333_33_333_33() -> None:
    amounts = _trial(_three_units(), _fields(AllocationKind.EQUAL, "1000"))
    assert amounts == [Decimal("333.34"), Decimal("333.33"), Decimal("333.33")]
    assert sum(amounts, ZERO) == Decimal("1000")


def test_kural_bolum_tipi_katsayisi_ve_sabit_tutar(site: ChargingSite) -> None:
    fields = _fields(
        AllocationKind.FIXED_PER_UNIT,
        "500",
        type_coefficients={TYPE_21: Decimal("1.5"), TYPE_11: Decimal("1.0")},
    )
    amounts = _trial(site.units, fields)
    assert sorted(set(amounts)) == [Decimal("500.00"), Decimal("750.00")]


def test_kural_muafiyet_indirimi_ve_toplam_kontrolu(site: ChargingSite) -> None:
    exempt = site.unit("A-1").id
    fields = _fields(
        AllocationKind.EQUAL, "2400", exemptions={exempt: Exemption(Decimal(50), "boş daire")}
    )
    rule_item = to_item(
        uuid.uuid7(), "Aidat", fields, payer_rule=PayerRule.OCCUPANT, owner_item=False
    )
    data = ChargeInput(date(2026, 11, 1), date(2026, 11, 10), [rule_item], site.units, [], [])
    result = simulate(data, rule_item)
    line = next(line for unit, line in result.lines if unit.id == exempt)
    assert line.amount == Decimal("50.00")
    assert "%50 muafiyet (boş daire)" in line.explanation
    assert result.total.exempted == Decimal("50.00")
    assert result.total.distributed + result.total.exempted == result.total.amount


def test_kural_malik_kalemi_malike_yazilir(site: ChargingSite) -> None:
    fields = _fields(AllocationKind.EQUAL, "2400")
    rule_item = to_item(
        uuid.uuid7(), "Bahçe", fields, payer_rule=PayerRule.OCCUPANT, owner_item=True
    )
    data = replace(site.input(rule_item), charge_date=date(2026, 11, 1))
    preview = build_preview(data)
    a2 = [c for c in preview.charges if c.unit_name == "A-2"]
    assert [c.account_kind for c in a2] == [AccountKind.OWNER]
    assert a2[0].lines[0].source is ItemSource.BILLING_RULE


def test_kural_dogrulama() -> None:
    errors = check_version(
        _fields(
            AllocationKind.COMPOSITE,
            "0",
            note="x",
            components=(ComponentData(AllocationKind.BY_AREA, Decimal(70)),),
        )
    )
    assert set(errors) == {"amount", "basis_note", "components"}
    assert check_version(_fields(AllocationKind.BY_METER_CONSUMPTION, "10"))["kind"]
    assert check_version(_fields(AllocationKind.EQUAL, "1000")) == {}


def test_kapsam_cakismasi() -> None:
    whole = _fields(AllocationKind.EQUAL, "1")
    block_a = _fields(
        AllocationKind.EQUAL, "1", scope_kind=ScopeKind.BLOCKS, scope_block_ids=frozenset({BLOCK_A})
    )
    block_b = _fields(
        AllocationKind.EQUAL,
        "1",
        scope_kind=ScopeKind.BLOCKS,
        scope_block_ids=frozenset({uuid.uuid7()}),
    )
    assert scopes_overlap(whole, block_a)
    assert not scopes_overlap(block_a, block_b)


# --- Gecikme (FIN-03, docs/04 §6, docs/07 §3.11–3.12) -------------------------------------------

RULE = LateFeeRule(monthly_rate_percent=Decimal(5), grace_days=5)


def test_3_11_gecikme_tazminati_25() -> None:
    policy = LateFeePolicyData(grace_days=5)
    assert late_fee(Decimal(1000), date(2026, 6, 10), date(2026, 6, 30), policy) == Decimal("25.00")
    assert accrued_late_fee(
        Decimal(1000), date(2026, 6, 10), [], date(2026, 6, 30), RULE
    ) == Decimal("25.00")


def test_3_12_tolerans_icinde_tazminat_yok() -> None:
    assert accrued_late_fee(Decimal(1000), date(2026, 6, 10), [], date(2026, 6, 14), RULE) == ZERO
    assert accrued_late_fee(Decimal(1000), date(2026, 6, 10), [], date(2026, 6, 15), RULE) == ZERO


def test_fin03_s1_gun_bazli_15_gun_25_s2_tam_ay_50() -> None:
    no_grace = LateFeeRule(Decimal(5), 0)
    assert accrued_late_fee(
        Decimal(1000), date(2026, 6, 1), [], date(2026, 6, 16), no_grace
    ) == Decimal("25.00")
    full = LateFeeRule(Decimal(5), 0, method="full_month")
    assert accrued_late_fee(
        Decimal(1000), date(2026, 6, 1), [], date(2026, 6, 16), full
    ) == Decimal("50.00")
    assert accrued_late_fee(Decimal(1000), date(2026, 6, 1), [], date(2026, 7, 2), full) == Decimal(
        "100.00"
    )


def test_kismi_odemede_kalan_ana_borc_uzerinden() -> None:
    """1.000 TL; 10. gecikme gününde 600 ödendi → 10 gün × 1.000 + 5 gün × 400."""
    rule = LateFeeRule(Decimal(3), 0)
    fee = accrued_late_fee(
        Decimal(1000),
        date(2026, 6, 1),
        [(date(2026, 6, 11), Decimal(600))],
        date(2026, 6, 16),
        rule,
    )
    assert fee == Decimal("12.00")  # 1000×0,03/30×10 + 400×0,03/30×5 = 10 + 2


def test_tavan_asgari_ve_yururluk() -> None:
    capped = LateFeeRule(Decimal(5), 0, cap_percent=1)
    assert accrued_late_fee(
        Decimal(1000), date(2026, 1, 1), [], date(2026, 6, 1), capped
    ) == Decimal("10.00")
    minimum = LateFeeRule(Decimal(5), 0, minimum_amount=Decimal(50))
    assert accrued_late_fee(Decimal(1000), date(2026, 6, 1), [], date(2026, 6, 16), minimum) == ZERO
    # yöntem 11 Haziran'da yürürlüğe girdi: öncesine işletilmez
    fee = accrued_late_fee(
        Decimal(1000), date(2026, 6, 1), [], date(2026, 6, 16), RULE, start_floor=date(2026, 6, 11)
    )
    assert fee == Decimal("10.00")


def test_tamamen_odenmis_borca_odeme_gununden_sonra_islemez() -> None:
    paid = [(date(2026, 6, 20), Decimal(1000))]
    fee = accrued_late_fee(Decimal(1000), date(2026, 6, 10), paid, date(2026, 7, 31), RULE)
    assert fee == Decimal("8.33")  # 16–20 Haziran: 5 gün


# --- Taslak anlık görüntüsü (FIN-05 S1) --------------------------------------------------------


def test_taslak_goruntusu_birebir_geri_okunur(site: ChargingSite) -> None:
    site.end_parties("A-2", date(2027, 1, 15), PartyRole.TENANT)
    preview = build_preview(site.input(item("Aidat", "240000", AllocationKind.EQUAL)))
    loaded, not_due = snapshot.load(snapshot.dump(preview, ["Çatı"]))
    assert loaded == preview
    assert not_due == ["Çatı"]


def test_ayni_girdi_ayni_sonuc(site: ChargingSite) -> None:
    """FIN-05 S1: aynı kural ve aynı veriyle taslak iki kez hesaplanınca birebir aynı."""
    data = site.input(item("Aidat", "240000", AllocationKind.EQUAL))
    assert build_preview(data) == build_preview(data)
