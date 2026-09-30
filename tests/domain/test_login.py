"""Giriş kilidi — docs/05 §8: 5 hatalı denemede 15 dk kilit."""

from datetime import UTC, datetime, timedelta

from site_yonetim.domain.login import (
    LOCK_DURATION,
    LoginState,
    normalize_email,
    password_problem,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def test_dort_hatada_kilit_yok_besincide_15_dk_kilit() -> None:
    state = LoginState(0, None)
    for _ in range(4):
        state = state.after_failure(NOW)
    assert not state.is_locked(NOW)
    assert state.failed_count == 4

    state = state.after_failure(NOW)

    assert state.is_locked(NOW)
    assert state.locked_until == NOW + timedelta(minutes=15)
    assert state.failed_count == 0


def test_kilit_suresi_dolunca_acilir() -> None:
    state = LoginState(0, NOW + LOCK_DURATION)

    assert state.is_locked(NOW + LOCK_DURATION - timedelta(seconds=1))
    assert not state.is_locked(NOW + LOCK_DURATION)


def test_basarili_giris_sayaci_sifirlar() -> None:
    assert LoginState.after_success() == LoginState(0, None)


def test_eposta_kucuk_harf_ve_kirpilir() -> None:
    assert normalize_email("  Ayse.Yilmaz@Example.COM ") == "ayse.yilmaz@example.com"


def test_parola_en_az_8_karakter() -> None:
    assert password_problem("1234567") == "Parola en az 8 karakter olmalı."
    assert password_problem("12345678") is None
