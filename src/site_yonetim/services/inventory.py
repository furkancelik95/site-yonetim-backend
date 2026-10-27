"""Demirbaş ve stok — frontend servis isteği 17. Açık site kapsamında; transaction'ı çağıran
yönetir.

- Demirbaş kodu sitede sıralı (`DB-0001`): işlem kilidi altında `max + 1`, `(site, sequence)`
  benzersizliği ikinci güvence. Kod değişmez; demirbaş silinmez, `retired` yapılır.
- Stok miktarı hareketle **aynı işlemde**, malzeme satırı kilitliyken güncellenir; eşzamanlı iki
  çıkış sıraya girer, ikincisi güncel miktarı görür. Eksiye düşmeyi veritabanı kısıtı da engeller.
- Hareket değişmez (tetikleyici); yanlışsa ters hareket. Stok girişi gider yazmaz.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.approvals import ActionType, ApprovalStatus
from site_yonetim.domain.facility import STOCK_COUNT_NOTE, OpsError, StockCountStatus
from site_yonetim.domain.management import (
    AssetStatus,
    StockDirection,
    asset_code,
    format_quantity,
    format_quantity_tr,
    parse_quantity,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import Asset, Site, StockCount, StockCountLine, StockItem, StockMove
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services.approvals import ExecutionError

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
QUANTITY_ERROR = "Sıfırdan büyük bir miktar girin (en çok 3 ondalık)."


def _clean(value: str | None, limit: int = 200) -> str:
    return " ".join((value or "").split())[:limit]


# --- Demirbaş -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetData:
    name: str | None
    category: str | None
    location: str | None
    acquired_on: date | None
    value: Decimal | None
    status: str | None
    assignee: str | None
    note: str | None


def check_asset(data: AssetData) -> AssetData:
    errors: dict[str, str] = {}
    name = _clean(data.name)
    if len(name) < 2:
        errors["name"] = "Demirbaşın adını yazın."
    if data.value is not None and data.value < 0:
        errors["value"] = "Bedel sıfır ya da daha büyük olmalı."
    if data.status not in {s.value for s in AssetStatus}:
        errors["status"] = "Durumu seçin."
    if errors:
        raise FormFieldError(errors)
    return AssetData(
        name=name,
        category=_clean(data.category) or None,
        location=_clean(data.location) or None,
        acquired_on=data.acquired_on,
        value=data.value,
        status=data.status,
        assignee=_clean(data.assignee) or None,
        note=_clean(data.note, 1000) or None,
    )


def current_asset(item: Asset) -> AssetData:
    return AssetData(
        name=item.name,
        category=item.category,
        location=item.location,
        acquired_on=item.acquired_on,
        value=item.value,
        status=item.status,
        assignee=item.assignee,
        note=item.note,
    )


def _apply_asset(item: Asset, data: AssetData) -> None:
    item.name = data.name or ""
    item.category = data.category
    item.location = data.location
    item.acquired_on = data.acquired_on
    item.value = data.value
    item.status = data.status or AssetStatus.IN_USE.value
    item.assignee = data.assignee
    item.note = data.note


async def create_asset(session: AsyncSession, data: AssetData) -> Asset:
    clean = check_asset(data)
    scope = current_scope()
    await session.execute(_NUMBER_LOCK, {"key": f"assets:{scope.site_id if scope else ''}"})
    sequence = ((await session.scalar(select(func.max(Asset.sequence)))) or 0) + 1
    item = Asset(id=uuid.uuid7(), sequence=sequence, code=asset_code(sequence))
    _apply_asset(item, clean)
    session.add(item)
    await session.flush()
    return item


async def update_asset(session: AsyncSession, item: Asset, data: AssetData) -> Asset:
    _apply_asset(item, check_asset(data))
    await session.flush()
    return item


async def get_asset(session: AsyncSession, asset_id: uuid.UUID) -> Asset | None:
    found: Asset | None = await session.scalar(
        select(Asset).where(Asset.id == asset_id).with_for_update()
    )
    return found


def assets_query(status: AssetStatus | None) -> Select[Asset]:
    query = select(Asset)
    if status is not None:
        query = query.where(Asset.status == status.value)
    return query.order_by(Asset.sequence)


# --- Stok ---------------------------------------------------------------------------


def _quantity(value: str | None, field: str, errors: dict[str, str], *, zero: bool) -> Decimal:
    try:
        return parse_quantity(value, allow_zero=zero)
    except ValueError:
        errors[field] = (
            QUANTITY_ERROR
            if not zero
            else "Sıfır ya da daha büyük bir miktar girin (en çok 3 ondalık)."
        )
        return Decimal(0)


async def create_item(
    session: AsyncSession,
    *,
    name: str | None,
    unit_label: str | None,
    min_quantity: str | None,
    location: str | None,
) -> StockItem:
    errors: dict[str, str] = {}
    label = _clean(name, 120)
    if len(label) < 2:
        errors["name"] = "Malzemenin adını yazın."
    elif await session.scalar(select(StockItem.id).where(StockItem.name_key == ascii_fold(label))):
        errors["name"] = "Bu adla bir malzeme var."
    unit = _clean(unit_label, 20)
    if not unit:
        errors["unit_label"] = "Birimi yazın (adet, litre, kg…)."
    minimum = _quantity(min_quantity or "0", "min_quantity", errors, zero=True)
    if errors:
        raise FormFieldError(errors)
    item = StockItem(
        id=uuid.uuid7(),
        name=label,
        name_key=ascii_fold(label),
        unit_label=unit,
        quantity=Decimal(0),
        min_quantity=minimum,
        location=_clean(location) or None,
    )
    session.add(item)
    await session.flush()
    return item


async def get_item(
    session: AsyncSession, item_id: uuid.UUID, *, lock: bool = False
) -> StockItem | None:
    query = select(StockItem).where(StockItem.id == item_id)
    found: StockItem | None = await session.scalar(query.with_for_update() if lock else query)
    return found


def items_query() -> Select[StockItem]:
    return select(StockItem).order_by(StockItem.name_key, StockItem.id)


def moves_query(item_id: uuid.UUID) -> Select[StockMove]:
    return (
        select(StockMove)
        .where(StockMove.stock_item_id == item_id)
        .order_by(StockMove.created_at.desc(), StockMove.id.desc())
    )


async def move(
    session: AsyncSession,
    item: StockItem,
    *,
    direction: str | None,
    quantity: str | None,
    note: str | None,
    moved_by: str,
) -> StockMove:
    """`item` kilitli (`get_item(lock=True)`) gelmeli."""
    errors: dict[str, str] = {}
    if direction not in {d.value for d in StockDirection}:
        errors["direction"] = "Giriş mi çıkış mı, seçin."
    amount = _quantity(quantity, "quantity", errors, zero=False)
    if errors:
        raise FormFieldError(errors)
    if direction == StockDirection.OUT.value and amount > item.quantity:
        raise OperationRuleError(
            "insufficient_stock",
            f"Stokta {format_quantity_tr(item.quantity)} {item.unit_label} var; "
            "daha fazlası çıkarılamaz.",
            conflict=True,
        )
    item.quantity += amount if direction == StockDirection.IN.value else -amount
    record = StockMove(
        id=uuid.uuid7(),
        stock_item_id=item.id,
        direction=direction,
        quantity=amount,
        note=_clean(note, 500) or None,
        moved_by=moved_by,
    )
    session.add(record)
    await session.flush()
    return record


# --- Stok sayımı (OPS-02; servis isteği 54) -----------------------------------------
#
# Yalnız girilen malzemeler sayılır. Fark ≠ 0 olan satırlar "Sayım farkı" notlu düzeltme
# hareketi olur (fazla → giriş, eksik → çıkış), tek işlemde ve malzeme satırları kilitliyken.
# Sitede "Stok sayım farkı" onay kuralı yürürlükteyse hareketler **onaydan sonra** yazılır
# (K21; ONY-01): sayım `approval_pending` kalır, onaylanınca yürütücü farkı uygular. Fark, sayım
# anındaki sistem miktarına göre hesaplanır ve onayda **fark olarak** uygulanır; stok bu arada
# düşüp çıkış yetmezse "onaylandı – yürütülemedi".


@dataclass(frozen=True, slots=True)
class CountLineData:
    item_id: uuid.UUID
    counted: str | None


@dataclass(frozen=True, slots=True)
class CountResult:
    count: StockCount
    lines: list[StockCountLine]
    request: ApprovalRequest | None


def signed_quantity(value: Decimal) -> str:
    """signed_quantity(Decimal("-2.500")) → "-2.5" """
    return format_quantity(value) if value >= 0 else "-" + format_quantity(-value)


async def _locked_items(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, StockItem]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(StockItem).where(StockItem.id.in_(ids)).order_by(StockItem.id).with_for_update()
    )
    return {i.id: i for i in rows}


async def _apply_count(
    session: AsyncSession, lines: list[StockCountLine], *, moved_by: str
) -> None:
    changed = [line for line in lines if line.diff != 0]
    items = await _locked_items(session, [line.stock_item_id for line in changed])
    for line in changed:
        await move(
            session,
            items[line.stock_item_id],
            direction=(StockDirection.IN if line.diff > 0 else StockDirection.OUT).value,
            quantity=format_quantity(abs(line.diff)),
            note=STOCK_COUNT_NOTE,
            moved_by=moved_by,
        )


async def count_stock(
    session: AsyncSession,
    site: Site,
    lines: list[CountLineData],
    *,
    requester: approvals_svc.Person,
    today: date,
    now: datetime,
) -> CountResult:
    entered = list({line.item_id: line for line in lines if (line.counted or "").strip()}.values())
    if not entered:
        raise FormFieldError({"lines": "En az bir malzeme."})
    items = await _locked_items(session, [line.item_id for line in entered])
    if len(items) != len(entered):
        raise OpsError(404, "not_found", "Malzeme bulunamadı.")
    errors: dict[str, str] = {}
    counted: dict[uuid.UUID, Decimal] = {}
    for line in entered:
        try:
            counted[line.item_id] = parse_quantity(line.counted, allow_zero=True)
        except ValueError:
            errors[f"lines.{line.item_id}"] = "Geçersiz miktar."
    if errors:
        raise FormFieldError(errors)
    record = StockCount(
        id=uuid.uuid7(),
        status=StockCountStatus.APPLIED.value,
        created_by_name=requester.name,
        created_by_user_id=requester.user_id,
    )
    session.add(record)
    await session.flush()
    rows = [
        StockCountLine(
            id=uuid.uuid7(),
            count_id=record.id,
            stock_item_id=items[line.item_id].id,
            item_name=items[line.item_id].name,
            system_quantity=items[line.item_id].quantity,
            counted_quantity=counted[line.item_id],
            diff=counted[line.item_id] - items[line.item_id].quantity,
        )
        for line in entered
    ]
    session.add_all(rows)
    await session.flush()
    differences = [r for r in rows if r.diff != 0]
    rule = await approvals_svc.match_rule(
        session, ActionType.STOCK_COUNT_DIFFERENCE, amount=None, source=None, today=today
    )
    if differences and rule is not None:
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.STOCK_COUNT_DIFFERENCE,
                title=f"Stok sayım farkı — {len(differences)} malzeme",
                summary={
                    "lines": [
                        {
                            "name": r.item_name,
                            "system": format_quantity(r.system_quantity),
                            "counted": format_quantity(r.counted_quantity),
                            "diff": signed_quantity(r.diff),
                        }
                        for r in differences
                    ]
                },
                payload={"stock_count_id": str(record.id)},
                subject_type="stock_counts",
                subject_id=record.id,
            ),
            requester=requester,
            today=today,
            now=now,
        )
        record.status = StockCountStatus.APPROVAL_PENDING.value
        record.approval_request_id = request.id
        await session.flush()
        return CountResult(record, rows, request)
    await _apply_count(session, rows, moved_by=requester.name)
    record.applied_at = now
    await session.flush()
    return CountResult(record, rows, None)


def counts_query() -> Select[StockCount]:
    return select(StockCount).order_by(StockCount.created_at.desc(), StockCount.id.desc())


async def count_lines(
    session: AsyncSession, count_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[StockCountLine]]:
    out: dict[uuid.UUID, list[StockCountLine]] = {i: [] for i in count_ids}
    if count_ids:
        for line in await session.scalars(
            select(StockCountLine)
            .where(StockCountLine.count_id.in_(count_ids))
            .order_by(StockCountLine.item_name)
        ):
            out[line.count_id].append(line)
    return out


async def approval_statuses(
    session: AsyncSession, request_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not request_ids:
        return {}
    rows = await session.execute(
        select(ApprovalRequest.id, ApprovalRequest.status).where(
            ApprovalRequest.id.in_(request_ids)
        )
    )
    return {row[0]: row[1] for row in rows}


async def _count_of(session: AsyncSession, request: ApprovalRequest) -> StockCount | None:
    raw = (request.payload or {}).get("stock_count_id")
    if not raw:
        return None
    found: StockCount | None = await session.scalar(
        select(StockCount).where(StockCount.id == uuid.UUID(str(raw))).with_for_update()
    )
    return found


async def _execute_count(session: AsyncSession, request: ApprovalRequest) -> None:
    record = await _count_of(session, request)
    if record is None or record.status != StockCountStatus.APPROVAL_PENDING.value:
        raise ExecutionError("Sayım kaydı artık onay beklemiyor.")
    lines = (await count_lines(session, [record.id]))[record.id]
    try:
        await _apply_count(session, lines, moved_by=f"{record.created_by_name} (onaylı sayım)")
    except OperationRuleError as exc:
        raise ExecutionError(f"Sayım farkı yazılamadı: {exc.message}") from exc
    record.status = StockCountStatus.APPLIED.value
    record.applied_at = request.decided_at
    await session.flush()


async def _close_count(session: AsyncSession, request: ApprovalRequest) -> None:
    record = await _count_of(session, request)
    if record is None or record.status != StockCountStatus.APPROVAL_PENDING.value:
        return
    rejected = ApprovalStatus(request.status) is ApprovalStatus.REJECTED
    record.status = (StockCountStatus.REJECTED if rejected else StockCountStatus.WITHDRAWN).value
    await session.flush()


approvals_svc.register_executor(ActionType.STOCK_COUNT_DIFFERENCE, _execute_count)
approvals_svc.register_closer(ActionType.STOCK_COUNT_DIFFERENCE, _close_count)
