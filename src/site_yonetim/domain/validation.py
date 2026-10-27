"""Biçim doğrulamaları — saf. Hata mesajları Türkçe, `ValueError` olarak."""

import re

# docs/11 §3. Alan adı noktayla ayrılmış, boş olmayan parçalar: desen belirsiz değil, uzun
# girdide geri izleme patlaması (ReDoS) yok — herkese açık kayıt formu bunu kullanıcıdan alır.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
EMAIL_MAX = 254
_TR_IBAN = re.compile(r"^TR\d{24}$")


def normalize_email_address(value: str) -> str:
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not _EMAIL.match(email):
        raise ValueError("Geçerli bir e-posta adresi girin (ör. ad@alanadi.com).")
    return email


def normalize_iban(value: str) -> str:
    """TR IBAN: boşluklar atılır, büyük harf; 26 karakter ve ISO 13616 mod-97 kontrolü."""
    iban = "".join(value.split()).upper()
    if not _TR_IBAN.match(iban):
        raise ValueError("IBAN 'TR' ile başlamalı ve 26 karakter olmalı.")
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    if int(digits) % 97 != 1:
        raise ValueError("IBAN kontrol basamağı hatalı; lütfen numarayı kontrol edin.")
    return iban


def normalize_tax_number(value: str) -> str:
    """Vergi kimlik numarası (VKN): 10 hane."""
    vkn = "".join(value.split())
    if not (len(vkn) == 10 and vkn.isdigit()):
        raise ValueError("Vergi kimlik numarası 10 haneli olmalı.")
    return vkn


# --- Kişi (docs/03 §4, docs/11 §3) ------------------------------------------

NAME_MIN, NAME_MAX = 2, 40


def normalize_name_part(value: str, label: str) -> str:
    """Ad/soyad: 2–40 karakter, rakam yok. Biçim `normalize_person_name` ile verilir."""
    text = " ".join(value.split())
    if not NAME_MIN <= len(text) <= NAME_MAX:
        raise ValueError(f"{label} {NAME_MIN}–{NAME_MAX} karakter olmalı.")
    if any(ch.isdigit() for ch in text):
        raise ValueError(f"{label} rakam içeremez.")
    return text


def normalize_tr_mobile(value: str) -> str:
    """Telefon → E.164 (`+905321234567`). Yalnız rakamlar alınır; baştaki 90 ya da 0 atılır;
    kalan 10 hane 5 ile başlamalı (cep telefonu)."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 12 and digits.startswith("90"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or not digits.startswith("5"):
        raise ValueError(
            "Telefon 5 ile başlayan 10 haneli bir cep numarası olmalı (ör. 0532 123 45 67)."
        )
    return f"+90{digits}"


_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(value: str) -> str:
    """Giriş telefonu → E.164 (PLT-01: ülke kodu seçici, varsayılan +90; boşluk ve baştaki 0
    temizlenir). `+90` dışındaki ülke kodları genel E.164 kuralıyla kabul edilir."""
    text = "".join(ch for ch in value.strip() if ch.isdigit() or ch == "+")
    if text.startswith("+") and not text.startswith("+90"):
        if not _E164.match(text):
            raise ValueError("Telefon numarasını ülke koduyla girin (ör. +49 151 2345 6789).")
        return text
    return normalize_tr_mobile(text)
