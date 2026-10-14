"""Banka hareketi aktarımı — frontend servis isteği 06. Açık site kapsamında; transaction'ı
çağıran yönetir.

Akış Excel'den daire aktarımıyla aynı: **önizleme hiçbir şey yazmaz** (yalnız önizleme kaydı),
onay seçilen satırları tahsilat olarak işler.

- Dosya diske yazılmaz; okunan satırlar `bank_imports.rows`'ta 6 saat durur, onayda silinir.
- Önizlemeyi yalnız yükleyen kullanıcı, aynı sitede onaylayabilir; başkası için "yok"tur.
- Onay **tek transaction**: her satır mevcut tahsilat servisiyle (FIFO mahsup, defter, kasaya
  giriş) kendi kayıt noktasında işlenir; işlenemeyen satır `skipped`'a düşer, diğerleri işlenir.
- Aynı banka hesabına aynı hareket ikinci kez aktarılamaz: `bank_import_lines` benzersiz
  parmak izi — iki ayrı önizleme aynı anda onaylansa da.
"""

import codecs
import csv
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import xlrd  # type: ignore[import-untyped]
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.bank import (
    AccountRef,
    BankFileError,
    BankRow,
    Direction,
    Matcher,
    MatchStatus,
    fingerprint,
    parse_statement,
)
from site_yonetim.domain.cash import CashAccountKind
from site_yonetim.domain.charging.payments import PaymentMethod
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.imports.numbers import CellValue
from site_yonetim.domain.imports.unit_validator import ImportFileError
from site_yonetim.domain.money import ZERO
from site_yonetim.models import AccountBalance, BankImport, BankImportLine, LedgerAccount, Person
from site_yonetim.services import accounts as account_svc
from site_yonetim.services import cash, payments
from site_yonetim.services.imports import open_sheet

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PENDING_TTL = timedelta(hours=6)
_XLS_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_SIGNATURE = b"PK\x03\x04"
_EXTENSIONS = (".xlsx", ".xls", ".csv")


# --- Dosya okuma ----------------------------------------------------------------------


def _xls_rows(data: bytes) -> list[list[CellValue]]:
    if not data.startswith(_XLS_SIGNATURE):
        raise _unreadable()
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        sheet = book.sheet_by_index(0)
        rows: list[list[CellValue]] = []
        for index in range(min(sheet.nrows, 6000)):
            row: list[CellValue] = []
            for cell in sheet.row(index)[:60]:
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        book.release_resources()
    except (xlrd.XLRDError, ValueError, IndexError, KeyError, OverflowError, AssertionError) as exc:
        raise _unreadable() from exc
    return rows


def _decode(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1254")  # Türk bankalarının eski dışa aktarımları


def _csv_rows(data: bytes) -> list[list[CellValue]]:
    text = _decode(data)
    sample = "\n".join(text.splitlines()[:30])
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = "excel"
    rows: list[list[CellValue]] = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    return rows[:6000]


def read_rows(file_name: str | None, data: bytes) -> list[BankRow]:
    name = (file_name or "").lower()
    if not name.endswith(_EXTENSIONS):
        raise BankFileError("invalid_file_type", "Yalnız .xlsx, .xls ya da .csv yüklenebilir.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BankFileError("file_too_large", "Dosya en fazla 5 MB olabilir.")
    if name.endswith(".xlsx"):
        if not data.startswith(_ZIP_SIGNATURE):
            raise _unreadable()
        try:
            with open_sheet(data) as sheet:
                cells: list[list[CellValue]] = [
                    list(r) for _, r in zip(range(6000), sheet, strict=False)
                ]
        except ImportFileError as exc:
            raise _unreadable() from exc
    elif name.endswith(".xls"):
        cells = _xls_rows(data)
    else:
        cells = _csv_rows(data)
    return parse_statement(cells)


def _unreadable() -> BankFileError:
    return BankFileError(
        "invalid_file_content",
        "Dosya okunamadı. Bankanın internet şubesinden Excel ya da CSV olarak yeniden indirin.",
    )


# --- Önizleme -----------------------------------------------------------------------


async def _account_refs(session: AsyncSession) -> list[AccountRef]:
    rows = await session.execute(
        select(
            LedgerAccount.id,
            LedgerAccount.reference_code,
            Person.id,
            Person.first_name,
            Person.last_name,
            AccountBalance.balance,
        )
        .join(
            Person,
            (Person.id == LedgerAccount.person_id) & (Person.site_id == LedgerAccount.site_id),
        )
        .outerjoin(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
        .where(LedgerAccount.is_closed.is_(False))
    )
    return [
        AccountRef(str(i), code, f"{first} {last}", str(person), balance or ZERO)
        for i, code, person, first, last, balance in rows
    ]


async def _imported(
    session: AsyncSession, cash_account_id: uuid.UUID, prints: Sequence[str]
) -> set[str]:
    if not prints:
        return set()
    found = await session.scalars(
        select(BankImportLine.fingerprint).where(
            BankImportLine.cash_account_id == cash_account_id,
            BankImportLine.fingerprint.in_(list(prints)),
        )
    )
    return set(found)


async def bank_account(session: AsyncSession, cash_account_id: uuid.UUID | None) -> Any:
    account = await cash.get_account(session, cash_account_id) if cash_account_id else None
    if account is None or not account.is_active or account.kind != CashAccountKind.BANK.value:
        raise FinanceRuleError(
            "bank_account_required", "Banka hesabını seçin.", field="cash_account_id"
        )
    return account


async def preview(
    session: AsyncSession,
    *,
    cash_account_id: uuid.UUID | None,
    file_name: str,
    rows: list[BankRow],
    user_id: uuid.UUID,
    now: datetime,
) -> BankImport:
    account = await bank_account(session, cash_account_id)
    await session.execute(
        delete(BankImport).where(BankImport.status == "pending", BankImport.expires_at <= now)
    )
    matcher = Matcher(await _account_refs(session))
    prints = [fingerprint(r) for r in rows]
    already = await _imported(session, account.id, prints)
    seen: set[str] = set()
    stored: list[dict[str, object]] = []
    for row, mark in zip(rows, prints, strict=True):
        if row.direction is Direction.OUT:
            match_status, suggestion = MatchStatus.IGNORED, None
        elif mark in already or mark in seen:
            match_status, suggestion = MatchStatus.DUPLICATE, None
        else:
            match = matcher.match(row)
            match_status = match.status
            suggestion = (
                {
                    "ledger_account_id": match.account_id,
                    "confidence": match.confidence,
                    "reason": match.reason,
                }
                if match.account_id
                else None
            )
        seen.add(mark)
        stored.append(
            {
                "row_number": row.row_number,
                "date": row.date.isoformat(),
                "description": row.description,
                "amount": f"{row.amount:.2f}",
                "direction": row.direction.value,
                "bank_reference": row.bank_reference,
                "fingerprint": mark,
                "status": match_status.value,
                "suggestion": suggestion,
            }
        )
    bank_import = BankImport(
        id=uuid.uuid7(),
        cash_account_id=account.id,
        file_name=file_name[:200],
        uploaded_by_user_id=user_id,
        expires_at=now + PENDING_TTL,
        status="pending",
        rows=stored,
        row_count=len(stored),
    )
    session.add(bank_import)
    await session.flush()
    return bank_import


async def pending(
    session: AsyncSession,
    import_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    now: datetime,
    lock: bool = False,
) -> BankImport | None:
    """Yükleyenin, süresi dolmamış önizlemesi; değilse `None` (404)."""
    query = select(BankImport).where(
        BankImport.id == import_id, BankImport.uploaded_by_user_id == user_id
    )
    found: BankImport | None = await session.scalar(query.with_for_update() if lock else query)
    if found is None or (found.status == "pending" and found.expires_at <= now):
        return None
    return found


# --- Onay ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skipped:
    row_number: int
    code: str
    message: str


@dataclass
class Outcome:
    created_payments: int = 0
    total_amount: Decimal = ZERO
    skipped: list[Skipped] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Selection:
    row_number: int
    ledger_account_id: uuid.UUID


async def confirm(
    session: AsyncSession,
    bank_import: BankImport,
    selections: Sequence[Selection],
    *,
    today: date,
    recorded_by: str,
    now: datetime,
) -> Outcome:
    if bank_import.status != "pending":
        raise FinanceRuleError("already_confirmed", "Bu aktarım zaten onaylandı.", conflict=True)
    rows = {int(str(r["row_number"])): r for r in bank_import.rows}
    outcome = Outcome()
    used: set[int] = set()
    for selection in selections:
        number = selection.row_number
        row = rows.get(number)
        if row is None or number in used:
            outcome.skipped.append(Skipped(number, "row_not_found", "Satır bulunamadı."))
            continue
        used.add(number)
        status = MatchStatus(str(row["status"]))
        if status is MatchStatus.IGNORED:
            outcome.skipped.append(
                Skipped(number, "ignored", "Çıkış hareketi tahsilat olarak işlenmez.")
            )
            continue
        if status is MatchStatus.DUPLICATE:
            outcome.skipped.append(Skipped(number, "duplicate", "Bu hareket daha önce aktarıldı."))
            continue
        account = await account_svc.account_row(session, selection.ledger_account_id)
        if account is None:
            outcome.skipped.append(Skipped(number, "account_not_found", "Cari hesap bulunamadı."))
            continue
        amount = Decimal(str(row["amount"]))
        try:
            async with session.begin_nested():
                recorded = await payments.record_payment(
                    session,
                    account.account,
                    amount=amount,
                    day=date.fromisoformat(str(row["date"])),
                    method=PaymentMethod.BANK_TRANSFER,
                    reference=str(row["bank_reference"]) if row["bank_reference"] else None,
                    note=str(row["description"])[:500] or None,
                    today=today,
                    recorded_by=recorded_by,
                    cash_account_id=bank_import.cash_account_id,
                )
                session.add(
                    BankImportLine(
                        cash_account_id=bank_import.cash_account_id,
                        fingerprint=str(row["fingerprint"]),
                        payment_id=recorded.payment.id,
                        bank_import_id=bank_import.id,
                    )
                )
                await session.flush()
        except FinanceRuleError as exc:
            outcome.skipped.append(Skipped(number, exc.code, exc.message))
            continue
        except IntegrityError:
            outcome.skipped.append(Skipped(number, "duplicate", "Bu hareket daha önce aktarıldı."))
            continue
        outcome.created_payments += 1
        outcome.total_amount += amount
    bank_import.status = "confirmed"
    bank_import.confirmed_at = now
    bank_import.rows = []  # gönderen adları vb. — onaydan sonra tutulmaz
    await session.flush()
    return outcome


async def purge_expired(factory: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
    """Süresi dolmuş, onaylanmamış önizlemeleri siler (kişisel veri: gönderen adları)."""
    with all_sites_scope():
        async with factory() as session, session.begin():
            result = await session.execute(
                delete(BankImport).where(
                    BankImport.status == "pending", BankImport.expires_at <= now
                )
            )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
