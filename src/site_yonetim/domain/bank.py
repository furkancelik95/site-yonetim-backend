"""Banka hareketi aktarımı — saf kurallar (frontend servis isteği 06).

Bankanın internet şubesinden indirilen ekstrenin satırları okunur ve cari hesaplarla eşleştirilir.
Dosya biçimi (xlsx/xls/csv) servis katmanında hücre değerlerine çevrilir; burada yalnız değerler.

**Okuma.** Sütun adları bankaya göre değişir; başlık satırı ilk 30 satırda aranır (önünde hesap
bilgisi satırları olabilir). Tarih + açıklama + tutar (ya da ayrı Borç/Alacak) zorunludur.
Tutar tr-TR biçiminde gelebilir (`1.234,56`). Yön: tutarın işareti ya da Borç/Alacak sütunu —
hesap sahibinin gözünden **Alacak = giriş**, **Borç = çıkış**. Tarihi olmayan satırlar (devreden
bakiye, toplam) atlanır.

**Eşleştirme** (yalnız giriş hareketleri):
1. Açıklamada bir hesabın referans kodu geçiyorsa (`A1-K`) → `matched`, yüksek güven.
2. Açıklamada tek bir hesabın kişi adı geçiyorsa (Türkçe harf ve büyük/küçük duyarsız) →
   `suggested`, orta güven. Adaylar **aynı kişinin** birden çok hesabıysa (dairesinde oturan
   malikin malik + oturan hesabı) ve yalnız biri borçluysa o önerilir.
3. Yoksa ya da farklı kişiler aday ise → `unmatched`. Tahmin uydurulmaz.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.imports.numbers import CellValue, cell_decimal, cell_text
from site_yonetim.domain.money import EPSILON, ZERO, round_money
from site_yonetim.domain.text import ascii_fold

HEADER_SCAN_ROWS = 30
MAX_ROWS = 5000
_NAME_MAX_WORDS = 4


class BankFileError(Exception):
    """Dosya okunamadı ya da beklenen sütunlar yok — 422 `fields.file`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class Column(StrEnum):
    DATE = "date"
    DESCRIPTION = "description"
    AMOUNT = "amount"
    DEBIT = "debit"  # Borç — çıkış
    CREDIT = "credit"  # Alacak — giriş
    REFERENCE = "reference"


# Başlıklar `ascii_fold` sonrası karşılaştırılır; ilk eşleşen sütun alınır.
_ALIASES: dict[Column, tuple[str, ...]] = {
    Column.DATE: ("tarih", "islem tarihi", "valor", "valor tarihi", "tarih/saat", "islem zamani"),
    Column.DESCRIPTION: ("aciklama", "islem aciklamasi", "aciklamalar", "detay", "islem detayi"),
    Column.AMOUNT: ("tutar", "islem tutari", "miktar", "tutar (tl)", "islem tutari (tl)"),
    Column.DEBIT: ("borc", "borc tutari", "cikis"),
    Column.CREDIT: ("alacak", "alacak tutari", "giris"),
    Column.REFERENCE: (
        "dekont no", "dekont", "referans", "referans no", "islem no", "fis no", "fis/dekont no",
    ),
}  # fmt: skip
_LABELS = {
    Column.DATE: "Tarih, İşlem Tarihi",
    Column.DESCRIPTION: "Açıklama, İşlem Açıklaması",
    Column.AMOUNT: "Tutar, İşlem Tutarı ya da ayrı Borç ve Alacak",
}


@dataclass(frozen=True, slots=True)
class BankRow:
    row_number: int  # dosyadaki veri satırı sırası, 1'den
    date: date
    description: str
    amount: Decimal  # her zaman pozitif; yön ayrı
    direction: Direction
    bank_reference: str | None


def _header_key(value: CellValue) -> str:
    return " ".join(ascii_fold(cell_text(value)).split())


def _columns(header: Sequence[CellValue]) -> dict[Column, int]:
    found: dict[Column, int] = {}
    keys = [_header_key(v) for v in header]
    for column, aliases in _ALIASES.items():
        for index, key in enumerate(keys):
            if key in aliases:
                found[column] = index
                break
    return found


def _usable(columns: dict[Column, int]) -> bool:
    has_amount = Column.AMOUNT in columns or Column.CREDIT in columns
    return Column.DATE in columns and Column.DESCRIPTION in columns and has_amount


def _missing(columns: dict[Column, int]) -> BankFileError:
    for column in (Column.DATE, Column.DESCRIPTION, Column.AMOUNT):
        present = column in columns or (column is Column.AMOUNT and Column.CREDIT in columns)
        if not present:
            name = {Column.DATE: "Tarih", Column.DESCRIPTION: "Açıklama"}.get(column, "Tutar")
            return BankFileError(
                "column_not_found",
                f"{name} sütunu bulunamadı. Beklenen başlıklar: {_LABELS[column]}.",
            )
    return BankFileError("column_not_found", "Başlık satırı bulunamadı.")  # pragma: no cover


_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%y")


def parse_date(value: CellValue) -> date | None:
    """Hücre → tarih; tarih değilse `None` (devreden bakiye, toplam satırı)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = cell_text(value)
    if not text:
        return None
    head = text.split(" ")[0].split("T")[0]
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(head, pattern).date()  # noqa: DTZ007 — yalnız tarih kısmı
        except ValueError:
            continue
    return None


def _decimal(value: CellValue) -> Decimal | None:
    try:
        number = cell_decimal(value)
    except ValueError:
        return None
    return round_money(number) if number is not None else None


def _amount(
    row: Sequence[CellValue], columns: dict[Column, int]
) -> tuple[Decimal, Direction] | None:
    def at(column: Column) -> CellValue:
        index = columns.get(column)
        return row[index] if index is not None and index < len(row) else None

    credit = _decimal(at(Column.CREDIT))
    debit = _decimal(at(Column.DEBIT))
    if credit is not None and credit != ZERO:
        return abs(credit), Direction.IN
    if debit is not None and debit != ZERO:
        return abs(debit), Direction.OUT
    amount = _decimal(at(Column.AMOUNT))
    if amount is None or amount == ZERO:
        return None
    return abs(amount), Direction.IN if amount > ZERO else Direction.OUT


def parse_statement(rows: Iterable[Sequence[CellValue]]) -> list[BankRow]:
    """Ekstre satırları → hareketler. Başlık bulunamazsa `BankFileError`."""
    iterator = iter(rows)
    best: dict[Column, int] = {}
    columns: dict[Column, int] | None = None
    for _ in range(HEADER_SCAN_ROWS):
        header = next(iterator, None)
        if header is None:
            break
        candidate = _columns(header)
        if _usable(candidate):
            columns = candidate
            break
        if len(candidate) > len(best):
            best = candidate
    if columns is None:
        raise _missing(best)

    parsed: list[BankRow] = []
    for row in iterator:

        def at(column: Column, r: Sequence[CellValue] = row) -> CellValue:
            index = columns.get(column)
            return r[index] if index is not None and index < len(r) else None

        day = parse_date(at(Column.DATE))
        money = _amount(row, columns)
        if day is None or money is None:
            continue
        if len(parsed) >= MAX_ROWS:
            raise BankFileError(
                "too_many_rows",
                f"Ekstre en fazla {MAX_ROWS} hareket içerebilir; tarihe göre bölün.",
            )
        reference = cell_text(at(Column.REFERENCE)) or None
        parsed.append(
            BankRow(
                row_number=len(parsed) + 1,
                date=day,
                description=cell_text(at(Column.DESCRIPTION))[:500],
                amount=money[0],
                direction=money[1],
                bank_reference=reference[:100] if reference else None,
            )
        )
    if not parsed:
        raise BankFileError("no_rows", "Dosyada aktarılacak hareket bulunamadı.")
    return parsed


def fingerprint(row: BankRow) -> str:
    """Mükerrer anahtarı: `(tarih, tutar, dekont no)`; dekont no yoksa açıklama."""
    third = row.bank_reference or " ".join(ascii_fold(row.description).split())
    raw = f"{row.date.isoformat()}|{row.amount:.2f}|{third}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- Eşleştirme ---------------------------------------------------------------------


class MatchStatus(StrEnum):
    MATCHED = "matched"  # yüksek güven — ekranda işaretli gelir
    SUGGESTED = "suggested"  # orta güven — kullanıcı bakar
    UNMATCHED = "unmatched"
    IGNORED = "ignored"  # çıkış hareketi, aktarılmaz
    DUPLICATE = "duplicate"  # aynı hareket daha önce aktarıldı


@dataclass(frozen=True, slots=True)
class AccountRef:
    id: str
    reference_code: str
    person_name: str
    person_id: str = ""
    balance: Decimal = ZERO  # pozitif = borçlu


@dataclass(frozen=True, slots=True)
class Match:
    status: MatchStatus
    account_id: str | None = None
    confidence: str | None = None  # high · medium
    reason: str | None = None


_TOKEN_SPLIT = re.compile(r"[^a-z0-9-]+")


def _tokens(text: str) -> list[str]:
    return [t.strip("-") for t in _TOKEN_SPLIT.split(ascii_fold(text)) if t.strip("-")]


@dataclass
class Matcher:
    accounts: Sequence[AccountRef]
    _codes: dict[str, set[str]] = field(default_factory=dict, init=False)
    _names: dict[str, set[str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for account in self.accounts:
            code = ascii_fold(account.reference_code)
            self._codes.setdefault(code, set()).add(account.id)
            name = " ".join(_tokens(account.person_name))
            if name.count(" ") >= 1:  # tek kelimelik ad eşleşme için yetersiz
                self._names.setdefault(name, set()).add(account.id)

    def match(self, row: BankRow) -> Match:
        if row.direction is Direction.OUT:
            return Match(MatchStatus.IGNORED)
        tokens = _tokens(row.description)
        by_code = {a for t in tokens for a in self._codes.get(t, ())}
        if len(by_code) == 1:
            [account] = by_code
            code = next(a.reference_code for a in self.accounts if a.id == account)
            return Match(
                MatchStatus.MATCHED, account, "high", f"Açıklamada referans kodu var ({code})"
            )
        if by_code:
            return Match(MatchStatus.UNMATCHED, reason="Açıklamada birden çok referans kodu var")
        by_name: set[str] = set()
        for size in range(2, _NAME_MAX_WORDS + 1):
            for start in range(len(tokens) - size + 1):
                by_name |= self._names.get(" ".join(tokens[start : start + size]), set())
        if len(by_name) == 1:
            [account] = by_name
            return Match(
                MatchStatus.SUGGESTED, account, "medium", "Gönderen adı hesap sahibiyle aynı"
            )
        refs = [a for a in self.accounts if a.id in by_name]
        debtors = [a for a in refs if a.balance > EPSILON]
        if len({a.person_id for a in refs}) == 1 and len(debtors) == 1:
            return Match(
                MatchStatus.SUGGESTED,
                debtors[0].id,
                "medium",
                "Gönderen adı hesap sahibiyle aynı; kişinin borçlu hesabı önerildi",
            )
        if by_name:
            return Match(MatchStatus.UNMATCHED, reason="Ad birden çok hesapla eşleşiyor")
        return Match(MatchStatus.UNMATCHED)
