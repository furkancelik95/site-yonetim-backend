"""Hizmet sözleşmeleri — frontend servis isteği 16. Açık site kapsamında; transaction'ı çağıran
yönetir.

- Sözleşme kaydı **gider yazmaz**; ödeme Giderler'den girilir.
- Silinmez, arşivlenir. `days_left` ve `state` saklanmaz, sitenin bugününe göre hesaplanır.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.management import ContractCategory, ContractPeriod
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Contract

NOTICE_MAX = 365


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


@dataclass(frozen=True, slots=True)
class ContractData:
    vendor: str | None
    subject: str | None
    category: str | None
    start_date: date | None
    end_date: date | None
    amount: Decimal | None
    period: str | None
    notice_days: int | None
    auto_renew: bool
    note: str | None


def check(data: ContractData) -> ContractData:
    """Alanları doğrular ve temizlenmiş halini döndürür; hatalar `FormFieldError`."""
    errors: dict[str, str] = {}
    vendor, subject = _clean(data.vendor), _clean(data.subject)
    if not 2 <= len(vendor) <= 200:
        errors["vendor"] = "Firma adını yazın."
    if not 2 <= len(subject) <= 300:
        errors["subject"] = "Sözleşmenin konusunu yazın."
    if data.category not in {c.value for c in ContractCategory}:
        errors["category"] = "Sözleşme türünü seçin."
    if data.start_date is None:
        errors["start_date"] = "Başlangıç tarihini seçin."
    if data.end_date is None:
        errors["end_date"] = "Bitiş tarihini seçin."
    elif data.start_date is not None and data.end_date < data.start_date:
        errors["end_date"] = "Bitiş, başlangıçtan önce olamaz."
    if data.amount is not None and data.amount < 0:
        errors["amount"] = "Tutar sıfır ya da daha büyük olmalı."
    if data.period is not None and data.period not in {p.value for p in ContractPeriod}:
        errors["period"] = "Ödeme sıklığını seçin."
    if data.notice_days is None or not 0 <= data.notice_days <= NOTICE_MAX:
        errors["notice_days"] = f"İhbar süresi 0–{NOTICE_MAX} gün olmalı."
    if errors:
        raise FormFieldError(errors)
    return ContractData(
        vendor=vendor,
        subject=subject,
        category=data.category,
        start_date=data.start_date,
        end_date=data.end_date,
        amount=data.amount,
        period=data.period,
        notice_days=data.notice_days,
        auto_renew=data.auto_renew,
        note=_clean(data.note)[:2000] or None,
    )


def current(item: Contract) -> ContractData:
    return ContractData(
        vendor=item.vendor,
        subject=item.subject,
        category=item.category,
        start_date=item.start_date,
        end_date=item.end_date,
        amount=item.amount,
        period=item.period,
        notice_days=item.notice_days,
        auto_renew=item.auto_renew,
        note=item.note,
    )


def _apply(item: Contract, data: ContractData) -> None:
    item.vendor = data.vendor or ""
    item.subject = data.subject or ""
    item.category = data.category or ""
    item.start_date = data.start_date or date.min
    item.end_date = data.end_date or date.min
    item.amount = data.amount
    item.period = data.period
    item.notice_days = data.notice_days or 0
    item.auto_renew = data.auto_renew
    item.note = data.note


async def create(session: AsyncSession, data: ContractData) -> Contract:
    item = Contract(id=uuid.uuid7(), is_archived=False)
    _apply(item, check(data))
    session.add(item)
    await session.flush()
    return item


async def update(
    session: AsyncSession, item: Contract, data: ContractData, *, archived: bool | None
) -> Contract:
    _apply(item, check(data))
    if archived is not None:
        item.is_archived = archived
    await session.flush()
    return item


async def get(session: AsyncSession, contract_id: uuid.UUID) -> Contract | None:
    found: Contract | None = await session.scalar(
        select(Contract).where(Contract.id == contract_id).with_for_update()
    )
    return found


def list_query(archived: bool) -> Select[Contract]:
    return (
        select(Contract)
        .where(Contract.is_archived.is_(archived))
        .order_by(Contract.end_date, Contract.vendor, Contract.id)
    )
