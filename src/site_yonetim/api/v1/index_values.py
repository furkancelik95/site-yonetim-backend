"""Endeks değerleri — servis isteği 70 (Yazılım Dokümanı 15.3, SAT-06). **Yalnız platform
yöneticisi; başkası 404.**

TÜİK'in açıkladığı yıllık TÜFE / ÜFE değişimi (%) aylık girilir; tüm tesislerde sözleşme
artışında kullanılır. Dönem geçmiş ya da bu ay olmalı; aynı dönem yeniden yazılabilir (düzeltme
yeni satırdır, kim girdiği saklanır). TÜİK'ten otomatik çekme Faz 2.
"""

import datetime as dt
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import FactoryDep, PlatformAdminDep, SessionDep, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.finance_ops_common import actor, form_error
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import IndexValue
from site_yonetim.services import contract_performance as svc

router = APIRouter(prefix="/platform/index-values", tags=["platform"])


class IndexValueOut(BaseModel):
    period: str
    tufe: Decimal = Field(description="yıllık TÜFE değişimi (%)")
    ufe: Decimal = Field(description="yıllık ÜFE değişimi (%)")
    entered_by: str
    entered_at: dt.datetime

    @classmethod
    def of(cls, row: IndexValue) -> IndexValueOut:
        return cls(
            period=row.period,
            tufe=row.tufe,
            ufe=row.ufe,
            entered_by=row.entered_by,
            entered_at=row.created_at,
        )


class IndexMissingOut(BaseModel):
    period: str = Field(description="girilmesi gereken endeks dönemi")
    contracts: int = Field(description="30 gün içinde artışı bu döneme bağlı sözleşme sayısı")
    sites: int


@router.get("", summary="Endeks değerleri (dönem başına geçerli değer, yeniden eskiye)")
async def list_index_values(
    _: PlatformAdminDep, session: SessionDep, params: Annotated[PageParams, Depends()]
) -> Page[IndexValueOut]:
    latest = svc.latest_index_query().subquery()
    total = await session.scalar(select(func.count()).select_from(latest)) or 0
    rows = await session.scalars(
        select(IndexValue)
        .join(latest, latest.c.id == IndexValue.id)
        .order_by(IndexValue.period.desc())
        .offset(params.offset)
        .limit(params.page_size)
    )
    return Page(
        items=[IndexValueOut.of(r) for r in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


@router.get("/missing", summary="Eksik endeks uyarısı (30 gün içindeki artışlar)")
async def missing_index_values(
    _: PlatformAdminDep, factory: FactoryDep, today: TodayDep
) -> list[IndexMissingOut]:
    return [
        IndexMissingOut(period=m.period, contracts=m.contracts, sites=m.sites)
        for m in await svc.missing_indexes(factory, today=today)
    ]


class IndexValueIn(BaseModel):
    tufe: Decimal | None = None
    ufe: Decimal | None = None


@router.put("/{period}", summary="Dönemin endeks değerini gir / düzelt")
async def put_index_value(
    period: str,
    body: IndexValueIn,
    current: PlatformAdminDep,
    session: SessionDep,
    today: TodayDep,
) -> Written[IndexValueOut]:
    """422 `fields.period` (geçmiş ya da bu ay), `fields.tufe`, `fields.ufe`."""
    try:
        row = await svc.save_index(
            session, period, body.tufe, body.ufe, actor=actor(current), today=today
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await session.refresh(row, ["created_at"])
    out = IndexValueOut.of(row)
    await session.commit()
    return Written(
        data=out,
        message=f"{period} endeks değerleri kaydedildi; tüm tesislerde artış hesaplarında "
        "kullanılır.",
    )
