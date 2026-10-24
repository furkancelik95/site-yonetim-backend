"""Bildirim kategori × kanal kuralları — saf (domain/notifications.py, Yazılım Dokümanı PLT-05)."""

import pytest

from site_yonetim.domain.notifications import (
    NotificationCategory,
    NotificationChannel,
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
        C.URGENT: {NotificationChannel.PUSH, NotificationChannel.SMS},
        C.FINANCIAL: {NotificationChannel.PUSH, NotificationChannel.EMAIL},
        C.SECURITY: {NotificationChannel.PUSH},
        C.OPERATIONS: {NotificationChannel.PUSH},
        C.GOVERNANCE: {
            NotificationChannel.PUSH,
            NotificationChannel.EMAIL,
            NotificationChannel.SMS,
        },
        C.ANNOUNCEMENT: {NotificationChannel.PUSH},
    }


def test_kayitli_tercih_varsayilanin_ustune_yazilir() -> None:
    matrix = preference_matrix({(C.OPERATIONS, NotificationChannel.PUSH): False})
    assert matrix[C.OPERATIONS][NotificationChannel.PUSH] is False
    assert matrix[C.ANNOUNCEMENT][NotificationChannel.PUSH] is True


@pytest.mark.parametrize(
    ("category", "channel", "locked"),
    [
        (C.URGENT, NotificationChannel.SMS, True),
        (C.URGENT, NotificationChannel.EMAIL, False),  # varsayılan değil: açılabilir, kilitli değil
        (C.GOVERNANCE, NotificationChannel.EMAIL, True),
        (C.FINANCIAL, NotificationChannel.PUSH, False),
        (C.OPERATIONS, NotificationChannel.PUSH, False),
    ],
)
def test_kilitli_hucreler(category: C, channel: NotificationChannel, locked: bool) -> None:
    assert is_locked(category, channel) is locked


def test_acil_kapatilamaz_ama_ek_kanal_acilabilir() -> None:
    with pytest.raises(PreferenceError, match="Acil bildirimleri bu kanalda kapatılamaz"):
        apply_changes(preference_matrix({}), {(C.URGENT, NotificationChannel.SMS): False})
    result = apply_changes(preference_matrix({}), {(C.URGENT, NotificationChannel.EMAIL): True})
    assert result[C.URGENT][NotificationChannel.EMAIL] is True


def test_finansalda_en_az_bir_kanal() -> None:
    matrix = preference_matrix({})
    switched = apply_changes(
        matrix,
        {
            (C.FINANCIAL, NotificationChannel.PUSH): False,
            (C.FINANCIAL, NotificationChannel.SMS): True,
        },
    )
    assert switched[C.FINANCIAL] == {
        NotificationChannel.PUSH: False,
        NotificationChannel.SMS: True,
        NotificationChannel.EMAIL: True,
    }
    with pytest.raises(PreferenceError, match="Finansal bildirimleri için en az bir kanal"):
        apply_changes(
            matrix,
            {
                (C.FINANCIAL, NotificationChannel.PUSH): False,
                (C.FINANCIAL, NotificationChannel.EMAIL): False,
            },
        )


def test_guvenlikte_tek_kanal_kapatilamaz() -> None:
    with pytest.raises(PreferenceError, match="Güvenlik bildirimleri için en az bir kanal"):
        apply_changes(preference_matrix({}), {(C.SECURITY, NotificationChannel.PUSH): False})


def test_operasyon_ve_duyuru_tamamen_kapatilabilir() -> None:
    result = apply_changes(
        preference_matrix({}),
        {
            (C.OPERATIONS, NotificationChannel.PUSH): False,
            (C.ANNOUNCEMENT, NotificationChannel.PUSH): False,
        },
    )
    assert not any(result[C.OPERATIONS].values())
    assert not any(result[C.ANNOUNCEMENT].values())


def test_hatali_degisiklik_matrisi_bozmaz() -> None:
    matrix = preference_matrix({})
    with pytest.raises(PreferenceError):
        apply_changes(
            matrix,
            {
                (C.OPERATIONS, NotificationChannel.PUSH): False,
                (C.URGENT, NotificationChannel.PUSH): False,
            },
        )
    assert matrix[C.OPERATIONS][NotificationChannel.PUSH] is True  # girdi değişmedi
