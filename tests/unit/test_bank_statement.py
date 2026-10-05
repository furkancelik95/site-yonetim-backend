"""Banka ekstresi okuma ve eşleştirme — saf kurallar (servis isteği 06)."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from site_yonetim.domain.bank import (
    AccountRef,
    BankFileError,
    BankRow,
    Direction,
    Matcher,
    MatchStatus,
    fingerprint,
    parse_date,
    parse_statement,
)
from site_yonetim.domain.imports.numbers import CellValue

MORNING = datetime(2026, 10, 3, 9, 12)  # noqa: DTZ001 — Excel hücresi saat dilimsizdir

# Biçim A — tek "Tutar" sütunu, işaretli; önde hesap bilgisi satırları (xlsx'ten gelen değerler)
FORMAT_A: list[list[CellValue]] = [
    ["Hesap Hareketleri"],
    ["Müşteri", "AKSU KONAKLARI SİTE YÖNETİMİ"],
    ["IBAN", "TR33 0006 1005 1978 6457 8413 26"],
    [],
    ["Tarih", "Açıklama", "Tutar", "Bakiye", "Dekont No"],
    [MORNING, "FAST Mehmet ERDOĞAN A1-K EKIM AIDAT", "11.618,90", "50.000,00", "FT2600002"],
    ["04.10.2026", "ELEKTRIK FATURASI OTOMATIK ODEME", "-32.319,00", "17.681,00", None],
    [None, "Devreden bakiye", None, "17.681,00", None],
    ["05.10.2026", "EFT AYSE YILMAZ AIDAT", 1250.5, "", "EF001"],
]  # fmt: skip

# Biçim B — ayrı Borç/Alacak sütunları; csv'den gelen metinler
FORMAT_B: list[list[CellValue]] = [
    ["İŞLEM TARİHİ", "AÇIKLAMA", "BORÇ", "ALACAK", "BAKİYE", "REFERANS NO"],
    ["03/10/2026", "HAVALE 1234567 NOLU HESAPTAN", "", "3.500,00", "3.500,00", "HV5512001"],
    ["03/10/2026", "KASA MASRAFI", "12,50", "", "3.487,50", ""],
]


def test_bicim_a_isaretli_tutar_ve_on_satirlar() -> None:
    rows = parse_statement(FORMAT_A)
    assert [(r.row_number, r.date, r.amount, r.direction) for r in rows] == [
        (1, date(2026, 10, 3), Decimal("11618.90"), Direction.IN),
        (2, date(2026, 10, 4), Decimal("32319.00"), Direction.OUT),
        (3, date(2026, 10, 5), Decimal("1250.50"), Direction.IN),
    ]
    assert rows[0].bank_reference == "FT2600002"
    assert rows[1].bank_reference is None


def test_bicim_b_borc_alacak_sutunlari() -> None:
    first, second = parse_statement(FORMAT_B)
    assert (first.amount, first.direction, first.bank_reference) == (
        Decimal("3500.00"), Direction.IN, "HV5512001"
    )  # fmt: skip
    assert (second.amount, second.direction, second.bank_reference) == (
        Decimal("12.50"), Direction.OUT, None
    )  # fmt: skip


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (["Açıklama", "Tutar"], "Tarih sütunu bulunamadı. Beklenen başlıklar: Tarih, İşlem"),
        (["Tarih", "Tutar"], "Açıklama sütunu bulunamadı. Beklenen başlıklar: Açıklama, İşlem"),
        (
            ["Tarih", "Açıklama", "Bakiye"],
            "Tutar sütunu bulunamadı. Beklenen başlıklar: Tutar, İşlem",
        ),
    ],
)
def test_eksik_sutun_hangisi_oldugunu_soyler(header: list[str], message: str) -> None:
    with pytest.raises(BankFileError) as exc:
        parse_statement([header, ["01.10.2026", "x", "1"]])
    assert exc.value.message.startswith(message)


def test_hareketsiz_dosya() -> None:
    with pytest.raises(BankFileError) as exc:
        parse_statement([["Tarih", "Açıklama", "Tutar"], [None, "Toplam", None]])
    assert exc.value.code == "no_rows"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("03.10.2026", date(2026, 10, 3)),
        ("03/10/2026 14:22", date(2026, 10, 3)),
        ("2026-10-03T08:00:00", date(2026, 10, 3)),
        ("03-10-2026", date(2026, 10, 3)),
        (date(2026, 10, 3), date(2026, 10, 3)),
        ("Devreden", None),
        (None, None),
    ],
)
def test_tarih_okuma(value: object, expected: date | None) -> None:
    assert parse_date(value) == expected  # type: ignore[arg-type]


ACCOUNTS = [
    AccountRef("a1k", "A1-K", "Mehmet ERDOĞAN"),
    AccountRef("a2o", "A2-O", "Ayşe YILMAZ"),
    AccountRef("a3o", "A3-O", "Ali Can", "p3"),
    AccountRef("a3m", "A3-M", "Ali Can", "p3"),  # aynı kişinin iki hesabı, ikisi de borçsuz
    AccountRef("a4o", "A4-O", "Zeynep Kaya", "p4", Decimal("1000.00")),
    AccountRef("a4m", "A4-M", "Zeynep Kaya", "p4"),  # aynı kişi: yalnız oturan hesabı borçlu
    AccountRef("a5o", "A5-O", "Can Er", "p5", Decimal("10.00")),
    AccountRef("a6o", "A6-O", "Can Er", "p6", Decimal("20.00")),  # adaş, farklı kişi
    AccountRef("b1o", "B1-O", "Veli"),  # tek kelimelik ad eşleşme için yetersiz
]


def row(description: str, direction: Direction = Direction.IN) -> BankRow:
    return BankRow(1, date(2026, 10, 3), description, Decimal("100.00"), direction, None)


@pytest.mark.parametrize(
    ("description", "status", "account"),
    [
        ("FAST Mehmet ERDOĞAN A1-K EKIM AIDAT", MatchStatus.MATCHED, "a1k"),
        ("havale a1-k/ekim", MatchStatus.MATCHED, "a1k"),
        ("EFT AYSE YILMAZ AIDAT", MatchStatus.SUGGESTED, "a2o"),
        ("EFT ali can aidat", MatchStatus.UNMATCHED, None),  # iki hesap, ikisi de borçsuz
        ("EFT ZEYNEP KAYA EKIM", MatchStatus.SUGGESTED, "a4o"),  # aynı kişinin borçlu hesabı
        ("HAVALE CAN ER", MatchStatus.UNMATCHED, None),  # adaş iki kişi
        ("A1-K ve A2-O birlikte", MatchStatus.UNMATCHED, None),  # iki referans kodu
        ("HAVALE 1234567 NOLU HESAPTAN", MatchStatus.UNMATCHED, None),
        ("VELI odeme", MatchStatus.UNMATCHED, None),
    ],
)
def test_eslestirme(description: str, status: MatchStatus, account: str | None) -> None:
    match = Matcher(ACCOUNTS).match(row(description))
    assert (match.status, match.account_id) == (status, account)


def test_cikis_hareketi_aktarilmaz() -> None:
    match = Matcher(ACCOUNTS).match(row("A1-K iade", Direction.OUT))
    assert match.status is MatchStatus.IGNORED


def test_eslesme_gerekcesi() -> None:
    match = Matcher(ACCOUNTS).match(row("FAST A1-K EKIM"))
    assert (match.confidence, match.reason) == ("high", "Açıklamada referans kodu var (A1-K)")


def test_mukerrer_anahtari() -> None:
    a = BankRow(1, date(2026, 10, 3), "FAST A1-K", Decimal("100.00"), Direction.IN, "FT1")
    b = BankRow(9, date(2026, 10, 3), "farklı açıklama", Decimal("100.00"), Direction.IN, "FT1")
    c = BankRow(1, date(2026, 10, 3), "Fast  a1-k", Decimal("100.00"), Direction.IN, None)
    d = BankRow(1, date(2026, 10, 3), "FAST A1-K", Decimal("100.00"), Direction.IN, None)
    assert fingerprint(a) == fingerprint(b)  # dekont no aynı → aynı hareket
    assert fingerprint(c) == fingerprint(d)  # dekont yok → açıklama (harf/boşluk duyarsız)
    assert fingerprint(a) != fingerprint(d)
