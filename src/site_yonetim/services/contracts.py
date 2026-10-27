"""Hizmet sözleşmeleri — frontend servis isteği 16. Açık site kapsamında; transaction'ı çağıran
yönetir.

- Sözleşme kaydı **gider yazmaz**; ödeme Giderler'den girilir.
- Silinmez, arşivlenir. `days_left` ve `state` saklanmaz, sitenin bugününe göre hesaplanır.
- **Bedel geçmişi** (Burhan'ın notu, servis isteği 70): bedel ya da ödeme sıklığı değişince eski
  bedel silinmez; `contract_amounts`'a `effective_from` ile yeni satır yazılır. O tarihten önceki
  hesaplar (karne aylık bedeli, artış öncesi bedel) eski bedelle yapılır. `contracts.amount` en
  son yazılan (en geç yürürlüğe giren) bedeldir; bugün geçerli bedel `amount_at` ile okunur.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.contract_performance import AmountSource
from site_yonetim.domain.management import ContractCategory, ContractPeriod
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Contract, ContractAmount
from site_yonetim.services.approvals import Person

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


async def create(
    session: AsyncSession, data: ContractData, *, actor: Person | None = None
) -> Contract:
    item = Contract(id=uuid.uuid7(), is_archived=False)
    _apply(item, check(data))
    session.add(item)
    await session.flush()
    if item.amount is not None:
        await record_amount(
            session,
            item,
            amount=item.amount,
            period=item.period,
            effective_from=item.start_date,
            source=AmountSource.INITIAL,
            actor=actor,
        )
    return item


async def update(
    session: AsyncSession,
    item: Contract,
    data: ContractData,
    *,
    archived: bool | None,
    actor: Person | None = None,
    today: date | None = None,
) -> Contract:
    """Bedel ya da sıklık değişirse bedel geçmişine bugünden (başlangıçtan önce değil) geçerli
    yeni satır yazılır; eski bedel yerinde kalır."""
    clean = check(data)
    rows = await amount_rows(session, item.id)
    # Form ya en son bedeli (sütun) ya da bugün geçerli bedeli gösterir: ikisi de "değişmedi".
    latest = (item.amount, item.period)
    unchanged = {latest}
    if rows and today is not None:
        unchanged.add(amount_at(rows, today))
    _apply(item, clean)
    if archived is not None:
        item.is_archived = archived
    if (clean.amount, clean.period) in unchanged:
        item.amount, item.period = latest
    await session.flush()
    if (clean.amount, clean.period) not in unchanged:
        start = item.start_date
        await record_amount(
            session,
            item,
            amount=clean.amount,
            period=clean.period,
            effective_from=start if not rows or today is None else max(today, start),
            source=AmountSource.EDIT if rows else AmountSource.INITIAL,
            actor=actor,
        )
    return item


# --- Bedel geçmişi ------------------------------------------------------------------------


async def amount_rows(session: AsyncSession, contract_id: uuid.UUID) -> list[ContractAmount]:
    """Bedel geçmişi, yürürlük sırasıyla (aynı gün: sonra yazılan sonra)."""
    return list(
        await session.scalars(
            select(ContractAmount)
            .where(ContractAmount.contract_id == contract_id)
            .order_by(ContractAmount.effective_from, ContractAmount.id)
        )
    )


def amount_at(rows: list[ContractAmount], day: date) -> tuple[Decimal | None, str | None]:
    """`day` günü geçerli (bedel, sıklık). İlk satırdan önceki gün için ilk satır kullanılır
    (başlangıçtan önceki hesap yoktur)."""
    current: ContractAmount | None = None
    for row in rows:
        if row.effective_from <= day:
            current = row
        else:
            break
    if current is None and rows:
        current = rows[0]
    return (current.amount, current.period) if current else (None, None)


async def current_amounts(
    session: AsyncSession, contract_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, tuple[Decimal | None, str | None]]:
    """Liste için: her sözleşmenin `day` günü geçerli bedeli (tek sorgu)."""
    if not contract_ids:
        return {}
    rows = await session.scalars(
        select(ContractAmount)
        .where(ContractAmount.contract_id.in_(contract_ids))
        .order_by(ContractAmount.contract_id, ContractAmount.effective_from, ContractAmount.id)
    )
    grouped: dict[uuid.UUID, list[ContractAmount]] = {}
    for row in rows:
        grouped.setdefault(row.contract_id, []).append(row)
    return {cid: amount_at(items, day) for cid, items in grouped.items()}


async def record_amount(
    session: AsyncSession,
    item: Contract,
    *,
    amount: Decimal | None,
    period: str | None,
    effective_from: date,
    source: AmountSource,
    actor: Person | None,
    note: str | None = None,
    escalation_id: uuid.UUID | None = None,
) -> ContractAmount:
    row = ContractAmount(
        id=uuid.uuid7(),
        contract_id=item.id,
        amount=amount,
        period=period,
        effective_from=effective_from,
        source=source.value,
        escalation_id=escalation_id,
        note=note,
        created_by_id=actor.user_id if actor else None,
        created_by=actor.name if actor else "Sistem",
    )
    session.add(row)
    await session.flush()
    latest = (await amount_rows(session, item.id))[-1]
    item.amount, item.period = latest.amount, latest.period
    await session.flush()
    return row


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
