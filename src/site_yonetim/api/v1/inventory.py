"""Demirbaş ve stok — frontend servis isteği 17 (backend #45).

Çekirdek (modül yok). Okuma `inventory.read` (Yönetici, Yönetim Kurulu, Muhasebe, Teknik),
yazma `inventory.manage` (Yönetici, Teknik — stok çıkışını teknik personel yapar).
Miktarlar JSON'da **metin** (`"4.5"`, en çok 3 ondalık); kayan nokta yok.
"""

import dataclasses
import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, SiteContext, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.management import AssetStatus, StockDirection, format_quantity
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Asset, StockItem, StockMove
from site_yonetim.services import inventory as svc

router = APIRouter(prefix="/sites/{slug}", tags=["demirbaş ve stok"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.INVENTORY_READ))]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.INVENTORY_MANAGE))]
LIST_MAX = 1000


# --- Demirbaş -----------------------------------------------------------------------


class AssetIn(BaseModel):
    name: str | None = Field(default=None, max_length=400)
    category: str | None = Field(default=None, max_length=400)
    location: str | None = Field(default=None, max_length=400)
    acquired_on: dt.date | None = None
    value: Money | None = None
    status: str | None = Field(default=AssetStatus.IN_USE.value, max_length=20)
    assignee: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2000)


class AssetPatch(AssetIn):
    status: str | None = Field(default=None, max_length=20)


class AssetOut(BaseModel):
    id: uuid.UUID
    code: str = Field(description="DB-0001 — sunucu verir, değişmez")
    name: str
    category: str | None
    location: str | None
    acquired_on: dt.date | None
    value: Money | None
    status: AssetStatus
    assignee: str | None
    note: str | None

    @classmethod
    def of(cls, item: Asset) -> AssetOut:
        return cls(
            id=item.id,
            code=item.code,
            name=item.name,
            category=item.category,
            location=item.location,
            acquired_on=item.acquired_on,
            value=item.value,
            status=AssetStatus(item.status),
            assignee=item.assignee,
            note=item.note,
        )


@router.get("/assets", summary=f"Demirbaşlar (koda göre; dizi, en çok {LIST_MAX})")
async def list_assets(
    ctx: Reader,
    status_: Annotated[AssetStatus | None, Query(alias="status")] = None,
) -> list[AssetOut]:
    rows = await ctx.session.scalars(svc.assets_query(status_).limit(LIST_MAX))
    return [AssetOut.of(a) for a in rows]


@router.post("/assets", status_code=status.HTTP_201_CREATED, summary="Demirbaş ekle")
async def create_asset(body: AssetIn, ctx: Manager) -> Written[AssetOut]:
    fields = {k: getattr(body, k) for k in AssetIn.model_fields}
    try:
        item = await svc.create_asset(ctx.session, svc.AssetData(**fields))
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = AssetOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.code} {item.name} eklendi.")


@router.patch("/assets/{asset_id}", summary="Demirbaşı düzenle (kısmi; kod değişmez)")
async def update_asset(asset_id: uuid.UUID, body: AssetPatch, ctx: Manager) -> Written[AssetOut]:
    item = await svc.get_asset(ctx.session, asset_id)
    if item is None:
        raise NotFoundError("Demirbaş bulunamadı.")
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        await svc.update_asset(
            ctx.session, item, dataclasses.replace(svc.current_asset(item), **changes)
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = AssetOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.code} {item.name} güncellendi.")


# --- Stok ---------------------------------------------------------------------------


class StockItemIn(BaseModel):
    name: str | None = Field(default=None, max_length=400)
    unit_label: str | None = Field(default=None, max_length=60)
    min_quantity: str | None = Field(default="0", max_length=30)
    location: str | None = Field(default=None, max_length=400)


class StockItemOut(BaseModel):
    id: uuid.UUID
    name: str
    unit_label: str
    quantity: str = Field(description='mevcut — ondalık metin ("4.5")')
    min_quantity: str
    is_low: bool = Field(description="mevcut ≤ asgari → Azaldı")
    location: str | None

    @classmethod
    def of(cls, item: StockItem) -> StockItemOut:
        return cls(
            id=item.id,
            name=item.name,
            unit_label=item.unit_label,
            quantity=format_quantity(item.quantity),
            min_quantity=format_quantity(item.min_quantity),
            is_low=item.quantity <= item.min_quantity,
            location=item.location,
        )


class MoveIn(BaseModel):
    direction: str | None = Field(default=None, max_length=10)
    quantity: str | None = Field(default=None, max_length=30)
    note: str | None = Field(default=None, max_length=1000)


class MoveOut(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    direction: StockDirection
    quantity: str
    note: str | None
    moved_at: dt.datetime
    moved_by: str | None

    @classmethod
    def of(cls, move: StockMove) -> MoveOut:
        return cls(
            id=move.id,
            item_id=move.stock_item_id,
            direction=StockDirection(move.direction),
            quantity=format_quantity(move.quantity),
            note=move.note,
            moved_at=move.created_at,
            moved_by=move.moved_by,
        )


class MovedOut(BaseModel):
    item: StockItemOut
    move: MoveOut


@router.get("/stock-items", summary=f"Stok malzemeleri (ada göre; dizi, en çok {LIST_MAX})")
async def list_stock_items(ctx: Reader) -> list[StockItemOut]:
    rows = await ctx.session.scalars(svc.items_query().limit(LIST_MAX))
    return [StockItemOut.of(i) for i in rows]


@router.post("/stock-items", status_code=status.HTTP_201_CREATED, summary="Malzeme ekle")
async def create_stock_item(body: StockItemIn, ctx: Manager) -> Written[StockItemOut]:
    try:
        item = await svc.create_item(
            ctx.session,
            name=body.name,
            unit_label=body.unit_label,
            min_quantity=body.min_quantity,
            location=body.location,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = StockItemOut.of(item)
    await ctx.session.commit()
    return Written(
        data=out, message=f"{item.name} eklendi. Mevcudu girmek için giriş hareketi yapın."
    )


async def _item(ctx: SiteContext, item_id: uuid.UUID, *, lock: bool = False) -> StockItem:
    item = await svc.get_item(ctx.session, item_id, lock=lock)
    if item is None:
        raise NotFoundError("Malzeme bulunamadı.")
    return item


@router.get("/stock-items/{item_id}/moves", summary="Malzemenin hareketleri (yeniden eskiye)")
async def list_moves(
    item_id: uuid.UUID,
    ctx: Reader,
    limit: Annotated[int, Query(ge=1, le=100, description="son N hareket")] = 10,
) -> list[MoveOut]:
    await _item(ctx, item_id)
    rows = await ctx.session.scalars(svc.moves_query(item_id).limit(limit))
    return [MoveOut.of(m) for m in rows]


@router.post(
    "/stock-items/{item_id}/moves",
    status_code=status.HTTP_201_CREATED,
    summary="Giriş ya da çıkış (stok eksiye düşemez)",
)
async def create_move(
    item_id: uuid.UUID, body: MoveIn, ctx: Manager, current: CurrentUserDep
) -> Written[MovedOut]:
    item = await _item(ctx, item_id, lock=True)
    try:
        move = await svc.move(
            ctx.session,
            item,
            direction=body.direction,
            quantity=body.quantity,
            note=body.note,
            moved_by=current.user.full_name,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise ApiError(HTTPStatus.CONFLICT, exc.code, exc.message) from exc
    await ctx.session.refresh(move, ["created_at"])
    out = MovedOut(item=StockItemOut.of(item), move=MoveOut.of(move))
    await ctx.session.commit()
    kind = "giriş" if move.direction == StockDirection.IN.value else "çıkış"
    return Written(data=out, message=f"{item.name}: {kind} kaydedildi.")
