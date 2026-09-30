"""Biçim doğrulamaları — saf. Hata mesajları Türkçe, `ValueError` olarak."""

import re

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # docs/11 §3
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
