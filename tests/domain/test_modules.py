"""Modül kapısı saf kuralları — docs/01 "Modüller/Planlar", docs/10 §3.2."""

import pytest

from site_yonetim.domain.modules import (
    CORE_MODULES,
    ModuleKey,
    ModuleRuleError,
    check_toggle,
    initial_state,
    plan_allows,
    statuses,
)

M = ModuleKey
# docs/01 plan tablosu
BASLANGIC = ["finance", "announcements", "requests"]
STANDART = [*BASLANGIC, "documents", "visitors", "surveys"]
PRO = [key.value for key in ModuleKey if key is not M.PORTFOLIO]


def test_cekirdek_yalniz_finans() -> None:
    assert frozenset({M.FINANCE}) == CORE_MODULES


def test_plansiz_site_yalniz_cekirdegi_kullanir() -> None:
    assert plan_allows(None) == {M.FINANCE}
    assert plan_allows([]) == {M.FINANCE}


def test_plan_bilinmeyen_anahtari_yok_sayar_cekirdegi_ekler() -> None:
    assert plan_allows(["requests", "muhasebe-sap"]) == {M.FINANCE, M.REQUESTS}


def test_baslangic_planinda_acik_baslayanlar() -> None:
    state = initial_state(BASLANGIC)

    assert {key for key, on in state.items() if on} == {M.FINANCE, M.ANNOUNCEMENTS, M.REQUESTS}
    assert set(state) == set(ModuleKey)  # her modül için bir satır


def test_standart_planda_dokuman_da_acik_digerleri_kapali() -> None:
    state = initial_state(STANDART)

    assert {key for key, on in state.items() if on} == {
        M.FINANCE,
        M.ANNOUNCEMENTS,
        M.REQUESTS,
        M.DOCUMENTS,
    }
    assert state[M.VISITORS] is False  # planda var ama kapalı başlar; yönetici açar


def test_plansiz_sitede_yalniz_finans_acik_baslar() -> None:
    assert {key for key, on in initial_state(None).items() if on} == {M.FINANCE}


def test_cekirdek_kapatilamaz() -> None:
    with pytest.raises(ModuleRuleError) as exc:
        check_toggle(M.FINANCE, False, PRO)

    assert exc.value.code == "core_module_cannot_be_disabled"
    assert "kapatılamaz" in exc.value.message


def test_planda_olmayan_modul_acilamaz() -> None:
    with pytest.raises(ModuleRuleError) as exc:
        check_toggle(M.VISITORS, True, BASLANGIC)

    assert exc.value.code == "module_not_in_plan"


def test_planda_olmayan_modul_kapatilabilir() -> None:
    check_toggle(M.VISITORS, False, BASLANGIC)


def test_planda_olan_modul_acilabilir() -> None:
    check_toggle(M.VISITORS, True, STANDART)
    check_toggle(M.FINANCE, True, None)


def test_durum_listesi_acik_ve_planda_bilgisi() -> None:
    by_key = {s.key: s for s in statuses({M.REQUESTS, M.VISITORS}, BASLANGIC)}

    assert by_key[M.FINANCE].available
    assert by_key[M.REQUESTS].available
    assert by_key[M.VISITORS].enabled
    assert not by_key[M.VISITORS].in_plan
    assert not by_key[M.VISITORS].available
    assert len(by_key) == len(ModuleKey)
