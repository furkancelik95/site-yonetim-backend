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
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.management import (
    AssetStatus,
    StockDirection,
    asset_code,
    format_quantity_tr,
    parse_quantity,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import Asset, StockItem, StockMove

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
