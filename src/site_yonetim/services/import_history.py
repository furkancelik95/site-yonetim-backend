"""Aktarım geçmişi, araç ve açılış bakiyesi aktarımı, geri alma (servis isteği 25, YAP-06).

Akış: **yükle → önizle** (dosya geçici depoya, parti `previewed`, 6 saat) → **onayla** (aynı
dosya yeniden doğrulanır, tek transaction) → **7 gün içinde geri al**.

- Araç satırı `Bölüm | Plaka | Marka | Renk`: hatalı satır aktarılmaz (kısmi aktarım). Başka
  bölümde etkin plaka aktarımda `force` sayılır; bölüm plaka sınırı yine uygulanır.
- Açılış bakiyesi satırı `Referans | Tutar | Tarih` (borç +, alacak −; `1.250,00` olur): **hiç
  hata olmamalı**; onayda eski sistem toplamı girilir, kuruşuna kadar tutmazsa aktarılmaz
  (mutabakat, YAP-06 S4). Her satır devir bakiye kuralından geçer (hesap başına bir kez).
- Geri alma: araç → pasif; bölüm aktarımı → oluşturulan kayıtlar silinir (sonradan kayıt
  bağlandıysa engellenir); açılış bakiyesi → ters kayıt (sonradan hareket girildiyse engellenir).
"""

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.imports.numbers import CellValue, cell_decimal, cell_text
from site_yonetim.domain.imports.unit_validator import ImportFileError, match_key
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.vehicles import (
    VehicleKind,
    VehicleStatus,
    display_plate,
    is_valid_plate,
    normalize_plate,
)
from site_yonetim.models import (
    Block,
    ImportBatch,
    LedgerAccount,
    LedgerEntry,
    Person,
    Unit,
    UnitParty,
    UnitType,
    Vehicle,
)
from site_yonetim.services import ledger, opening_balances, period_lock, vehicles
from site_yonetim.services.imports import MAX_UPLOAD_BYTES, PENDING_TTL, ZIP_SIGNATURE, open_sheet

ROLLBACK_WINDOW = timedelta(days=7)
MAX_ROWS = 5000
OPENING_NOTE = "Açılış bakiyesi aktarımı"
TYPE_LABELS = {"units": "bölüm", "vehicles": "araç", "opening_balances": "açılış bakiyesi"}


class ImportRuleError(Exception):
    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


# --- Dosya okuma ----------------------------------------------------------------------


def check_file(filename: str | None, data: bytes) -> str:
    """Uzantı ve içerik imzası; `xlsx` ya da `csv` döner."""
    name = (filename or "").lower()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportFileError("file_too_large", "Dosya en fazla 5 MB olabilir.")
    if name.endswith(".xlsx"):
        if not data.startswith(ZIP_SIGNATURE):
            raise ImportFileError(
                "invalid_file_content", "Dosya geçerli bir Excel (.xlsx) dosyası değil."
            )
        return "xlsx"
    if name.endswith(".csv"):
        return "csv"
    raise ImportFileError("invalid_file_type", "Yalnız .xlsx ya da .csv yüklenebilir.")


def read_rows(kind: str, data: bytes) -> list[tuple[CellValue, ...]]:
    """Senkron ve CPU'ya bağlı — uç nokta iş parçacığında çağırır."""
    if kind == "xlsx":
        with open_sheet(data) as rows:
            return [tuple(r) for _, r in zip(range(MAX_ROWS + 1), rows, strict=False)]
    for encoding in ("utf-8-sig", "cp1254"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover — cp1254 her baytı çözer
        raise ImportFileError("invalid_file_content", "Dosya okunamadı.")
    first = text.split("\n", 1)[0]
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [tuple(r) for _, r in zip(range(MAX_ROWS + 1), reader, strict=False)]


def _columns(header: tuple[CellValue, ...], wanted: dict[str, tuple[str, ...]]) -> dict[str, int]:
    keys = [match_key(cell_text(h)) for h in header]
    found: dict[str, int] = {}
    for name, aliases in wanted.items():
        for alias in aliases:
            key = match_key(alias)
            if key in keys:
                found[name] = keys.index(key)
                break
    return found


def _cell(row: tuple[CellValue, ...], index: int | None) -> CellValue:
    return row[index] if index is not None and index < len(row) else None


def _data_rows(rows: list[tuple[CellValue, ...]]) -> list[tuple[int, tuple[CellValue, ...]]]:
    return [
        (number, row)
        for number, row in enumerate(rows[1:], start=2)
        if any(cell_text(c) for c in row)
    ]


@dataclass(frozen=True, slots=True)
class Issue:
    row_number: int | None
    column: str | None
    message: str
    severity: str = "error"


@dataclass(slots=True)
class Preview:
    type: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    total_amount: Decimal | None = None

    @property
    def importable_count(self) -> int:
        return sum(1 for r in self.rows if r["ok"])


# --- Araç önizleme --------------------------------------------------------------------

_VEHICLE_COLUMNS = {
    "unit": ("Bölüm", "Bolum", "Daire", "Bölüm kodu", "Bölüm Kodu"),
    "plate": ("Plaka",),
    "brand": ("Marka", "Marka/Model", "Marka model", "Model"),
    "color": ("Renk",),
}


async def _unit_codes(session: AsyncSession) -> dict[str, tuple[uuid.UUID, str]]:
    rows = await session.execute(
        select(Unit.id, Unit.number, Block.name).join(
            Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id)
        )
    )
    codes: dict[str, tuple[uuid.UUID, str]] = {}
    for unit_id, number, block in rows:
        name = f"{block}-{number}" if block else number
        codes[match_key(name).replace(" ", "")] = (unit_id, name)
        codes[match_key(f"{block}{number}").replace(" ", "")] = (unit_id, name)
    return codes


async def preview_vehicles(session: AsyncSession, rows: list[tuple[CellValue, ...]]) -> Preview:
    if not rows:
        raise ImportFileError("invalid_file_content", "Dosya boş.")
    cols = _columns(rows[0], _VEHICLE_COLUMNS)
    if "unit" not in cols or "plate" not in cols:
        raise ImportFileError("invalid_file_content", "Başlıkta Bölüm ve Plaka sütunları gerekli.")
    codes = await _unit_codes(session)
    preview = Preview("vehicles")
    seen: set[tuple[uuid.UUID, str]] = set()
    for number, row in _data_rows(rows):
        unit_text = cell_text(_cell(row, cols["unit"]))
        unit = codes.get(match_key(unit_text).replace(" ", ""))
        plate = normalize_plate(cell_text(_cell(row, cols["plate"])))
        ok = True
        if unit is None:
            preview.issues.append(Issue(number, "Bölüm", f"Bölüm bulunamadı: {unit_text or '—'}"))
            ok = False
        if not is_valid_plate(plate):
            preview.issues.append(
                Issue(number, "Plaka", f"Plaka geçersiz: {cell_text(_cell(row, cols['plate']))}")
            )
            ok = False
        if ok and unit is not None:
            if (unit[0], plate) in seen:
                preview.issues.append(Issue(number, "Plaka", "Aynı plaka dosyada iki kez var."))
                ok = False
            seen.add((unit[0], plate))
        preview.rows.append(
            {
                "row_number": number,
                "unit_id": str(unit[0]) if unit else None,
                "unit_name": unit[1] if unit else unit_text,
                "plate": plate,
                "plate_display": display_plate(plate),
                "brand_model": cell_text(_cell(row, cols.get("brand"))) or None,
                "color": cell_text(_cell(row, cols.get("color"))) or None,
                "ok": ok,
            }
        )
    return preview


# --- Açılış bakiyesi önizleme ---------------------------------------------------------

_OPENING_COLUMNS = {
    "reference": ("Referans", "Referans kodu", "Hesap", "Hesap kodu"),
    "amount": ("Tutar", "Bakiye", "Borç"),
    "date": ("Tarih", "Bakiye tarihi"),
}


def _parse_day(value: CellValue, today: date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = cell_text(value)
    if not text:
        return today
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 — yalnız tarih
        except ValueError:
            continue
    raise ValueError(text)


async def preview_opening(
    session: AsyncSession, rows: list[tuple[CellValue, ...]], today: date
) -> Preview:
    if not rows:
        raise ImportFileError("invalid_file_content", "Dosya boş.")
    cols = _columns(rows[0], _OPENING_COLUMNS)
    if "reference" not in cols or "amount" not in cols:
        raise ImportFileError(
            "invalid_file_content", "Başlıkta Referans kodu ve Tutar sütunları gerekli."
        )
    accounts = {
        code.upper(): (account_id, number, block, first, last)
        for account_id, code, number, block, first, last in await session.execute(
            select(
                LedgerAccount.id,
                LedgerAccount.reference_code,
                Unit.number,
                Block.name,
                Person.first_name,
                Person.last_name,
            )
            .join(
                Unit, and_(Unit.id == LedgerAccount.unit_id, Unit.site_id == LedgerAccount.site_id)
            )
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .join(
                Person,
                and_(Person.id == LedgerAccount.person_id, Person.site_id == LedgerAccount.site_id),
            )
        )
    }
    has_opening = set(
        await session.scalars(
            select(LedgerEntry.account_id).where(LedgerEntry.source == LedgerSource.OPENING.value)
        )
    )
    preview = Preview("opening_balances", total_amount=ZERO)
    seen: set[uuid.UUID] = set()
    for number, row in _data_rows(rows):
        ref = cell_text(_cell(row, cols["reference"])).upper()
        account = accounts.get(ref)
        ok = True
        if account is None:
            preview.issues.append(Issue(number, "Referans", f"Hesap bulunamadı: {ref or '—'}"))
            ok = False
        elif account[0] in has_opening:
            preview.issues.append(
                Issue(number, "Referans", f"{ref} hesabına devir bakiye daha önce girilmiş.")
            )
            ok = False
        elif account[0] in seen:
            preview.issues.append(Issue(number, "Referans", f"{ref} dosyada iki kez var."))
            ok = False
        amount: Decimal | None = None
        try:
            amount = cell_decimal(_cell(row, cols["amount"]))
        except ValueError:
            amount = None
        if amount is None or amount == ZERO or amount != round_money(amount):
            raw = cell_text(_cell(row, cols["amount"]))
            preview.issues.append(Issue(number, "Tutar", f"Tutar okunamadı: {raw or '—'}"))
            ok = False
        try:
            day = _parse_day(_cell(row, cols.get("date")), today)
            if day > today:
                preview.issues.append(Issue(number, "Tarih", "Tarih bugünden sonra olamaz."))
                ok = False
        except ValueError:
            preview.issues.append(Issue(number, "Tarih", "Tarih GG.AA.YYYY biçiminde olmalı."))
            day = today
            ok = False
        if account is not None:
            seen.add(account[0])
        if ok and amount is not None and preview.total_amount is not None:
            preview.total_amount += amount
        unit_name = None
        person_name = None
        if account is not None:
            unit_name = f"{account[2]}-{account[1]}" if account[2] else account[1]
            person_name = f"{account[3]} {account[4]}"
        preview.rows.append(
            {
                "row_number": number,
                "account_id": str(account[0]) if account else None,
                "reference_code": ref,
                "unit_name": unit_name,
                "person_name": person_name,
                "amount": f"{abs(amount):.2f}" if amount is not None else None,
                "signed_amount": f"{amount:.2f}" if amount is not None else None,
                "direction": "credit" if amount is not None and amount < ZERO else "debit",
                "date": day.isoformat(),
                "ok": ok,
            }
        )
    return preview


# --- Parti --------------------------------------------------------------------------------


async def open_batch(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    type_: str,
    file_name: str,
    preview: Preview,
    user_id: uuid.UUID,
    user_name: str,
    now: datetime,
) -> ImportBatch:
    batch = ImportBatch(
        id=batch_id,
        type=type_,
        file_name=(file_name or "dosya")[:120],
        status="previewed",
        row_count=len(preview.rows),
        total_amount=preview.total_amount,
        user_id=user_id,
        created_by_name=user_name,
        expires_at=now + PENDING_TTL,
    )
    session.add(batch)
    await session.flush()
    return batch


@dataclass(slots=True)
class Outcome:
    created: int = 0
    skipped: list[str] = field(default_factory=list)


async def apply_vehicles(
    session: AsyncSession, batch: ImportBatch, preview: Preview, *, today: date
) -> Outcome:
    outcome = Outcome()
    ids: list[str] = []
    units = {
        u.id: u
        for u in await session.scalars(
            select(Unit).where(
                Unit.id.in_([uuid.UUID(r["unit_id"]) for r in preview.rows if r["ok"]])
            )
        )
    }
    for row in preview.rows:
        if not row["ok"]:
            continue
        unit = units.get(uuid.UUID(row["unit_id"]))
        if unit is None:  # pragma: no cover — önizlemeden bu yana silinemez
            continue
        try:
            async with session.begin_nested():
                vehicle = await vehicles.add(
                    session,
                    unit,
                    vehicles.NewVehicle(
                        plate=row["plate"],
                        kind=VehicleKind.PERMANENT,
                        brand_model=row["brand_model"],
                        color=row["color"],
                    ),
                    by_resident=False,
                    force=True,
                    created_by=batch.created_by_name,
                    today=today,
                    import_batch_id=batch.id,
                )
        except vehicles.VehicleRuleError as exc:
            outcome.skipped.append(f"{row['row_number']}. satır ({exc.message})")
            continue
        ids.append(str(vehicle.id))
    outcome.created = len(ids)
    batch.created = {"vehicles": ids}
    return outcome


async def apply_opening(
    session: AsyncSession,
    batch: ImportBatch,
    preview: Preview,
    *,
    reconciliation_total: Decimal | None,
    today: date,
) -> Outcome:
    if preview.issues:
        raise ImportRuleError(
            409,
            "rows_invalid",
            "Bakiye aktarımında tüm satırlar temiz olmalı; hatalı satırları düzeltip dosyayı "
            "yeniden yükleyin.",
        )
    if reconciliation_total is None:
        raise ImportRuleError(
            422,
            "validation",
            "Eski sistemdeki toplam bakiyeyi girin.",
            {"reconciliation_total": "Zorunlu."},
        )
    total = preview.total_amount or ZERO
    if total != reconciliation_total:
        from site_yonetim.domain.text import format_money_tr

        raise ImportRuleError(
            409,
            "reconciliation_mismatch",
            f"Yüklenen toplam ({format_money_tr(total)}) eski sistem toplamıyla "
            f"({format_money_tr(reconciliation_total)}) tutmuyor; fark "
            f"{format_money_tr(total - reconciliation_total)}. Fark bulunmadan aktarılamaz.",
        )
    accounts = {
        a.id: a
        for a in await session.scalars(
            select(LedgerAccount).where(
                LedgerAccount.id.in_([uuid.UUID(r["account_id"]) for r in preview.rows])
            )
        )
    }
    outcome = Outcome()
    entries: list[str] = []
    for row in preview.rows:
        account = accounts[uuid.UUID(row["account_id"])]
        try:
            entry = await opening_balances.record(
                session,
                account,
                amount=Decimal(row["amount"]),
                direction=opening_balances.OpeningDirection(row["direction"]),
                day=date.fromisoformat(row["date"]),
                note=f"{OPENING_NOTE} ({batch.file_name})",
                today=today,
            )
        except FinanceRuleError as exc:
            raise ImportRuleError(
                409, "rows_invalid", f"{row['row_number']}. satır: {exc.message}"
            ) from exc
        entries.append(str(entry.id))
    outcome.created = len(entries)
    batch.created = {"entries": entries}
    return outcome


def finish(batch: ImportBatch, outcome: Outcome, now: datetime) -> None:
    batch.status = "imported"
    batch.created_count = outcome.created
    batch.imported_at = now
    batch.rollback_until = now + ROLLBACK_WINDOW
    batch.expires_at = None


async def record_units_batch(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    file_name: str | None,
    row_count: int,
    created: dict[str, list[str]],
    user_id: uuid.UUID,
    user_name: str,
    now: datetime,
) -> None:
    """Bölüm aktarımı onaylanınca geçmişe yazılır (`type=units`)."""
    batch = ImportBatch(
        id=batch_id,
        type="units",
        file_name=(file_name or "daire-aktarimi.xlsx")[:120],
        status="imported",
        row_count=row_count,
        created_count=len(created.get("units", [])),
        created=created,
        user_id=user_id,
        created_by_name=user_name,
        imported_at=now,
        rollback_until=now + ROLLBACK_WINDOW,
    )
    session.add(batch)
    await session.flush()


# --- Geri alma ----------------------------------------------------------------------------


def _ids(batch: ImportBatch, key: str) -> list[uuid.UUID]:
    return [uuid.UUID(v) for v in (batch.created or {}).get(key, [])]


async def blocked_by(session: AsyncSession, batch: ImportBatch) -> str | None:
    """Geri almayı engelleyen sonradan girilmiş kayıt (açıklama) ya da None."""
    if batch.type == "opening_balances":
        entries = _ids(batch, "entries")
        if not entries:
            return None
        accounts = select(LedgerEntry.account_id).where(LedgerEntry.id.in_(entries))
        later = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(
                LedgerEntry.account_id.in_(accounts),
                LedgerEntry.source != LedgerSource.OPENING.value,
            )
        )
        if later:
            return (
                f"Aktarılan hesaplara sonradan {later} hareket (tahakkuk, tahsilat…) girildi; "
                "geri alınamaz. Düzeltmeyi hesap bazında ters kayıtla yapın."
            )
        return None
    if batch.type == "units":
        account_ids = _ids(batch, "accounts")
        if account_ids:
            later = await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.account_id.in_(account_ids))
            )
            if later:
                return (
                    f"Aktarılan bölümlerin hesaplarına {later} hareket (tahakkuk, tahsilat…) "
                    "girildi; geri alınamaz."
                )
        units = _ids(batch, "units")
        persons = _ids(batch, "persons")
        parties = _ids(batch, "parties")
        extra = await session.scalar(
            select(func.count())
            .select_from(UnitParty)
            .where(
                UnitParty.unit_id.in_(units) | UnitParty.person_id.in_(persons),
                UnitParty.id.not_in(parties),
            )
        )
        if extra:
            return (
                f"Aktarılan bölüm ya da kişilere sonradan {extra} ilişki (malik, kiracı…) "
                "eklendi; geri alınamaz."
            )
    return None


async def rollback(session: AsyncSession, batch: ImportBatch, *, today: date, now: datetime) -> int:
    """Partinin oluşturduğu kayıtları kaldırır; kaldırılan kayıt sayısını döner."""
    if batch.status != "imported":
        raise ImportRuleError(409, "not_imported", "Bu aktarım zaten geri alınmış.")
    if batch.rollback_until is None or batch.rollback_until < now:
        raise ImportRuleError(409, "rollback_expired", "Geri alma süresi (7 gün) doldu.")
    reason = await blocked_by(session, batch)
    if reason is not None:
        raise ImportRuleError(409, "rollback_blocked", reason)
    removed = 0
    if batch.type == "vehicles":
        for vehicle in await session.scalars(
            select(Vehicle).where(
                Vehicle.id.in_(_ids(batch, "vehicles")),
                Vehicle.status != VehicleStatus.INACTIVE.value,
            )
        ):
            vehicle.status = VehicleStatus.INACTIVE.value
            removed += 1
    elif batch.type == "opening_balances":
        entries = list(
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.id.in_(_ids(batch, "entries")))
            )
        )
        await period_lock.ensure_writable(session, today)
        for entry in entries:  # düzeltme: kilitli döneme ait açılış kaydı geri alınamaz
            await period_lock.ensure_writable(session, entry.date, original=True)
        await ledger.lock_accounts(session, [e.account_id for e in entries])
        for entry in entries:
            session.add(
                LedgerEntry(
                    id=uuid.uuid7(),
                    account_id=entry.account_id,
                    date=today,
                    due_date=None,
                    debit=entry.credit,
                    credit=entry.debit,
                    source=LedgerSource.ADJUSTMENT.value,
                    source_id=batch.id,
                    description=f"Açılış bakiyesi aktarımı geri alındı ({batch.file_name})",
                    reversal_of_entry_id=entry.id,
                )
            )
            removed += 1
        await session.flush()
        await ledger.refresh_balances(session, list({e.account_id for e in entries}))
    else:
        try:
            async with session.begin_nested():
                removed = await _remove_units(session, batch)
        except IntegrityError as exc:
            raise ImportRuleError(
                409,
                "rollback_blocked",
                "Aktarılan bölüm ya da kişilere sonradan kayıt bağlandı (talep, ziyaretçi, "
                "kargo, başvuru…); geri alınamaz.",
            ) from exc
    batch.status = "rolled_back"
    batch.rolled_back_at = now
    await session.flush()
    return removed


async def _remove_units(session: AsyncSession, batch: ImportBatch) -> int:
    """Bölüm aktarımının oluşturduğu kayıtlar silinir (finansal kayıt yok — `blocked_by`)."""
    from site_yonetim.models import AccountBalance

    accounts = _ids(batch, "accounts")
    if accounts:
        await session.execute(delete(AccountBalance).where(AccountBalance.account_id.in_(accounts)))
        await session.execute(delete(LedgerAccount).where(LedgerAccount.id.in_(accounts)))
    parties = _ids(batch, "parties")
    if parties:
        await session.execute(delete(UnitParty).where(UnitParty.id.in_(parties)))
    persons = _ids(batch, "persons")
    if persons:
        await session.execute(delete(Person).where(Person.id.in_(persons)))
    units = _ids(batch, "units")
    if units:
        await session.execute(delete(Unit).where(Unit.id.in_(units)))
    types = _ids(batch, "unit_types")
    if types:
        await session.execute(
            delete(UnitType).where(
                UnitType.id.in_(types),
                ~select(Unit.id)
                .where(Unit.unit_type_id == UnitType.id)
                .correlate(UnitType)
                .exists(),
            )
        )
    blocks = _ids(batch, "blocks")
    if blocks:
        await session.execute(
            delete(Block).where(
                Block.id.in_(blocks),
                ~select(Unit.id).where(Unit.block_id == Block.id).correlate(Block).exists(),
            )
        )
    await session.flush()
    return len(units)


def can_rollback(batch: ImportBatch, now: datetime) -> bool:
    return (
        batch.status == "imported"
        and batch.rollback_until is not None
        and batch.rollback_until >= now
    )
