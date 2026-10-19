"""Bildirim kategori × kanal kuralları — saf (domain/notifications.py, Yazılım Dokümanı PLT-05)."""

import pytest

from site_yonetim.domain.notifications import (
    Channel,
    NotificationCategory,
    PreferenceError,
    apply_changes,
    is_locked,
    preference_matrix,
)

C = NotificationCategory


def test_varsayilanlar_dokumandaki_matris() -> None:
    matrix = preference_matrix({})
    on = {c: {ch for ch, enabled in row.items() if enabled} for c, row in matrix.items()}
    assert on == {
        C.URGENT: {Channel.PUSH, Channel.SMS},
        C.FINANCIAL: {Channel.PUSH, Channel.EMAIL},
        C.SECURITY: {Channel.PUSH},
        C.OPERATIONS: {Channel.PUSH},
        C.GOVERNANCE: {Channel.PUSH, Channel.EMAIL, Channel.SMS},
        C.ANNOUNCEMENT: {Channel.PUSH},
    }


def test_kayitli_tercih_varsayilanin_ustune_yazilir() -> None:
    matrix = preference_matrix({(C.OPERATIONS, Channel.PUSH): False})
    assert matrix[C.OPERATIONS][Channel.PUSH] is False
    assert matrix[C.ANNOUNCEMENT][Channel.PUSH] is True


@pytest.mark.parametrize(
    ("category", "channel", "locked"),
    [
        (C.URGENT, Channel.SMS, True),
        (C.URGENT, Channel.EMAIL, False),  # varsayılan değil: açılabilir, kilitli değil
        (C.GOVERNANCE, Channel.EMAIL, True),
        (C.FINANCIAL, Channel.PUSH, False),
        (C.OPERATIONS, Channel.PUSH, False),
    ],
)
def test_kilitli_hucreler(category: C, channel: Channel, locked: bool) -> None:
    assert is_locked(category, channel) is locked


def test_acil_kapatilamaz_ama_ek_kanal_acilabilir() -> None:
    with pytest.raises(PreferenceError, match="Acil bildirimleri bu kanalda kapatılamaz"):
        apply_changes(preference_matrix({}), {(C.URGENT, Channel.SMS): False})
    result = apply_changes(preference_matrix({}), {(C.URGENT, Channel.EMAIL): True})
    assert result[C.URGENT][Channel.EMAIL] is True


def test_finansalda_en_az_bir_kanal() -> None:
    matrix = preference_matrix({})
    switched = apply_changes(
        matrix, {(C.FINANCIAL, Channel.PUSH): False, (C.FINANCIAL, Channel.SMS): True}
    )
    assert switched[C.FINANCIAL] == {Channel.PUSH: False, Channel.SMS: True, Channel.EMAIL: True}
    with pytest.raises(PreferenceError, match="Finansal bildirimleri için en az bir kanal"):
        apply_changes(
            matrix, {(C.FINANCIAL, Channel.PUSH): False, (C.FINANCIAL, Channel.EMAIL): False}
        )


def test_guvenlikte_tek_kanal_kapatilamaz() -> None:
    with pytest.raises(PreferenceError, match="Güvenlik bildirimleri için en az bir kanal"):
        apply_changes(preference_matrix({}), {(C.SECURITY, Channel.PUSH): False})


def test_operasyon_ve_duyuru_tamamen_kapatilabilir() -> None:
    result = apply_changes(
        preference_matrix({}),
        {(C.OPERATIONS, Channel.PUSH): False, (C.ANNOUNCEMENT, Channel.PUSH): False},
    )
    assert not any(result[C.OPERATIONS].values())
    assert not any(result[C.ANNOUNCEMENT].values())


def test_hatali_degisiklik_matrisi_bozmaz() -> None:
    matrix = preference_matrix({})
    with pytest.raises(PreferenceError):
        apply_changes(
            matrix, {(C.OPERATIONS, Channel.PUSH): False, (C.URGENT, Channel.PUSH): False}
        )
    assert matrix[C.OPERATIONS][Channel.PUSH] is True  # girdi değişmedi
