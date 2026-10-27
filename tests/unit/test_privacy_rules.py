"""KVKK, saklama ve rapor kataloğu — saf kurallar (RAP-01, RAP-04; P6, K8)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.access import ROLE_PERMISSIONS, Permission, SiteRole
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.privacy import (
    PrivacyFieldError,
    PrivacyOutcome,
    PrivacyRequestKind,
    check_answer,
    check_notice,
    check_request,
    days_left,
    due_on,
    needs_warning,
)
from site_yonetim.domain.report_catalog import CATALOG, METRICS, SlaCounts, sla_line, visible
from site_yonetim.domain.retention import (
    BY_KEY,
    CATEGORIES,
    RetentionAction,
    RetentionFieldError,
    check_value,
    cutoff,
)


def test_basvuru_aciklamasi_erisim_disinda_zorunlu() -> None:
    assert check_request("access", None).detail == ""
    with pytest.raises(PrivacyFieldError) as error:
        check_request("erase", "  kısa ")
    assert error.value.fields == {"detail": "Talebinizi kısaca açıklayın."}
    with pytest.raises(PrivacyFieldError) as error:
        check_request("delete", "x" * 20)
    assert set(error.value.fields) == {"kind"}


def test_silmede_saklanan_kayit_ve_dayanagi() -> None:
    with pytest.raises(PrivacyFieldError) as error:
        check_answer(PrivacyRequestKind.ERASE, "Kısmen silindi, açıkladık.", "partial", " ")
    assert set(error.value.fields) == {"retained"}
    ok = check_answer(PrivacyRequestKind.ERASE, "Tamamı silindi, bildirdik.", "fulfilled", None)
    assert ok.outcome is PrivacyOutcome.FULFILLED
    other = check_answer(PrivacyRequestKind.OBJECT, "İtiraz değerlendirildi.", "rejected", None)
    assert other.retained is None


def test_aydinlatma_metni_en_az_200_karakter() -> None:
    with pytest.raises(PrivacyFieldError) as error:
        check_notice("a" * 199, "özet")
    assert error.value.fields == {
        "text": "Aydınlatma metni çok kısa (en az 200 karakter).",
        "summary": "Değişiklik özetini yazın.",
    }
    assert check_notice(" " + "a" * 200 + "\n", "Yeni sürüm").text == "a" * 200


def test_otuz_gun_sayaci_ve_uyari() -> None:
    assert due_on(date(2026, 6, 20)) == date(2026, 7, 20)
    assert days_left(date(2026, 7, 20), date(2026, 7, 22)) == -2
    assert not needs_warning(date(2026, 7, 20), date(2026, 7, 12))
    assert needs_warning(date(2026, 7, 20), date(2026, 7, 13))


def test_saklama_varsayilanlari_dokumandan() -> None:
    assert (BY_KEY["visitors"].default_value, BY_KEY["visitors"].max_value) == (6, 24)
    assert BY_KEY["emergency"].default_value == 30
    keep = [c.key for c in CATEGORIES if c.action is RetentionAction.KEEP]
    assert keep == ["finance", "employment"]
    # Hukuki teyit bekleyen her ayarlanabilir kategori: otomatik varsayılan kapalı (servis katmanı)
    assert all(c.needs_legal_review for c in CATEGORIES if c.adjustable)


def test_saklama_suresi_ve_kesim_tarihi() -> None:
    visitors = BY_KEY["visitors"]
    assert cutoff(visitors, 6, date(2026, 8, 31)) == date(2026, 2, 28)
    assert cutoff(BY_KEY["emergency"], 30, date(2026, 6, 20)) == date(2026, 5, 21)
    assert cutoff(BY_KEY["finance"], 10, date(2026, 6, 20)) == date(2016, 6, 20)
    with pytest.raises(RetentionFieldError):
        check_value(visitors, 25)
    with pytest.raises(RetentionFieldError):
        check_value(visitors, True)
    assert check_value(visitors, 24) == 24


def test_katalog_yetkiye_gore() -> None:
    def keys(role: SiteRole, modules: set[ModuleKey]) -> set[str]:
        return {r.key for r in visible(ROLE_PERMISSIONS[role], modules)}

    every = set(ModuleKey)
    assert "security" not in keys(SiteRole.BOARD_MEMBER, every)  # RAP-01 S1
    assert {r.group for r in visible(ROLE_PERMISSIONS[SiteRole.AUDITOR], every)} == {
        "Finans", "Satın alma", "Yönetim",
    }  # fmt: skip
    assert "requests_sla" not in keys(SiteRole.MANAGER, every - {ModuleKey.REQUESTS})
    assert keys(SiteRole.RESIDENT, every) == set()
    assert all(k in METRICS for r in CATALOG for k in r.metric_keys)
    assert Permission.PRIVACY_MANAGE in ROLE_PERMISSIONS[SiteRole.MANAGER]
    assert Permission.PRIVACY_MANAGE not in ROLE_PERMISSIONS[SiteRole.BOARD_MEMBER]


def test_sla_satiri() -> None:
    line = sla_line(
        SlaCounts(total=4, open=1, closed=3, with_sla=3, met=2, seconds=Decimal(3600 * 7),
                  timed=2, score_sum=9, rated=2)
    )  # fmt: skip
    assert (line.sla_rate, line.avg_hours, line.satisfaction) == (
        Decimal("66.7"), Decimal("3.5"), Decimal("4.5")
    )  # fmt: skip
    empty = sla_line(SlaCounts())
    assert (empty.sla_rate, empty.avg_hours, empty.satisfaction, empty.rated) == (None,) * 3 + (0,)
    total = SlaCounts(total=1, met=1) + SlaCounts(total=2, rated=1)
    assert (total.total, total.met, total.rated) == (3, 1, 1)
