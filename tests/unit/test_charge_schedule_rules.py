"""Otomatik tahakkuk takvimi — saf kurallar (servis isteği 04)."""

from datetime import date

import pytest

from site_yonetim.domain.charging.schedule import check_settings, is_due, next_run_on
from site_yonetim.domain.finance import FinanceRuleError

OCT_2 = date(2026, 10, 2)


@pytest.mark.parametrize(
    ("charge_day", "due_days", "field"),
    [(0, 14, "charge_day"), (29, 14, "charge_day"), (1, -1, "due_days"), (1, 61, "due_days")],
)
def test_ayar_sinirlari(charge_day: int, due_days: int, field: str) -> None:
    with pytest.raises(FinanceRuleError) as exc:
        check_settings(charge_day, due_days)
    assert exc.value.field == field


def test_sinirlar_dahil() -> None:
    check_settings(1, 0)
    check_settings(28, 60)


def test_kesim_gunu_geldiyse_ya_da_gectiyse_keser() -> None:
    assert is_due(OCT_2, 1, date(2026, 9, 1))  # dün kaçırıldı → bugün yetişir
    assert is_due(OCT_2, 2, date(2026, 9, 1))
    assert not is_due(OCT_2, 3, date(2026, 9, 1))  # gün gelmedi


def test_acildigi_gunden_onceki_kesim_gunu_geriye_donuk_kesilmez() -> None:
    assert not is_due(OCT_2, 1, OCT_2)  # 2 Ekim'de açıldı, kesim günü 1
    assert is_due(OCT_2, 2, OCT_2)  # aynı gün açıldı ve kesim günü bugün


def test_siradaki_calisma() -> None:
    enabled = date(2026, 9, 1)
    assert next_run_on(OCT_2, 1, enabled, settled=True) == date(2026, 11, 1)
    assert next_run_on(OCT_2, 1, enabled, settled=False) == OCT_2  # bekleyen: sıradaki çalışma
    assert next_run_on(OCT_2, 15, enabled, settled=False) == date(2026, 10, 15)
    assert next_run_on(OCT_2, 1, OCT_2, settled=False) == date(2026, 11, 1)  # geriye dönük yok
    assert next_run_on(date(2026, 12, 20), 5, enabled, settled=True) == date(2027, 1, 5)
