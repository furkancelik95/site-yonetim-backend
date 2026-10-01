"""Kasa ve banka servisleri — docs/04 §10. Açık site kapsamında; commit çağırana ait.

Her hareket `add_movement` üzerinden yazılır: hesap aktif mi bakılır, özet satırı
(`cash_balances`) **kilitlenir** ve aynı transaction'da artımlı güncellenir. Satır kilidi
eşzamanlı iki hareketin birbirinin güncellemesini ezmesini önler (docs/08 §2).
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, and_, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import TenantScopeError, current_scope
from site_yonetim.domain.cash import (
    CashAccountKind,
    CashSource,
    Direction,
    MovementState,
    check_movement_reversible,
    check_not_future,
    check_positive,
    clean_account_name,
    clean_iban,
    clean_text,
    clip,
    inactive_account_error,
    opening_amounts,
)
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO
from site_yonetim.models import CashAccount, CashBalance, CashMovement

REFERENCE_MAX = 100
NOTE_MAX = 500

DEFAULT_ACCOUNTS = (("Banka Hesabı", CashAccountKind.BANK), ("Kasa", CashAccountKind.CASH))


def _site_id() -> uuid.UUID:
    scope = current_scope()
    if scope is None or scope.site_id is None:
        raise TenantScopeError("Kasa işlemi yalnız tek bir site kapsamında çalışır.")
    return scope.site_id


_ENSURE = text(
    """
    INSERT INTO cash_balances (id, site_id, cash_account_id, inflow_total, outflow_total, balance)
    SELECT row_id, :site_id, account_id, 0, 0, 0
    FROM unnest(CAST(:row_ids AS uuid[]), CAST(:account_ids AS uuid[])) AS t(row_id, account_id)
    ORDER BY account_id
    ON CONFLICT (cash_account_id) DO NOTHING
    """
)
_LOCK = text(
    """
    SELECT cash_account_id FROM cash_balances
    WHERE site_id = :site_id AND cash_account_id = ANY(CAST(:account_ids AS uuid[]))
    ORDER BY cash_account_id FOR UPDATE
    """
)
_APPLY = text(
    """
    UPDATE cash_balances SET
        inflow_total = inflow_total + :inflow,
        outflow_total = outflow_total + :outflow,
        balance = balance + :inflow - :outflow,
        updated_at = now()
    WHERE site_id = :site_id AND cash_account_id = :account_id
    """
)


async def lock_accounts(session: AsyncSession, account_ids: Collection[uuid.UUID]) -> None:
    """Özet satırlarını açar ve kimlik sırasıyla kilitler (aktarımda iki hesap — kilitlenme yok)."""
    ids = sorted(set(account_ids))
    site_id = _site_id()
    await session.execute(
        _ENSURE, {"site_id": site_id, "row_ids": [uuid.uuid7() for _ in ids], "account_ids": ids}
    )
    await session.execute(_LOCK, {"site_id": site_id, "account_ids": ids})


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> CashAccount | None:
    account: CashAccount | None = await session.scalar(
        select(CashAccount).where(CashAccount.id == account_id)
    )
    return account


async def active_account(session: AsyncSession, account_id: uuid.UUID | None) -> CashAccount:
    """Hareket yazılacak hesap: var olmalı (başka sitenin hesabı da "yok"tur) ve aktif olmalı."""
    account = await get_account(session, account_id) if account_id else None
    if account is None:
        raise FinanceRuleError(
            "cash_account_not_found", "Kasa/banka hesabı bulunamadı.", field="cash_account_id"
        )
    if not account.is_active:
        raise inactive_account_error()
    return account


async def add_movement(
    session: AsyncSession,
    account: CashAccount,
    *,
    day: date,
    inflow: Decimal = ZERO,
    outflow: Decimal = ZERO,
    description: str,
    source: CashSource,
    source_id: uuid.UUID | None = None,
    reference: str | None = None,
    reversal_of_id: uuid.UUID | None = None,
    created_by: str | None = None,
    lock: bool = True,
) -> CashMovement:
    if lock:
        await lock_accounts(session, [account.id])
    movement = CashMovement(
        id=uuid.uuid7(),
        cash_account_id=account.id,
        date=day,
        inflow=inflow,
        outflow=outflow,
        description=description,
        reference=reference,
        source=source.value,
        source_id=source_id,
        reversal_of_id=reversal_of_id,
        created_by_name=created_by,
    )
    session.add(movement)
    await session.flush()
    await session.execute(
        _APPLY,
        {"site_id": _site_id(), "account_id": account.id, "inflow": inflow, "outflow": outflow},
    )
    return movement


# --- Hesap -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewAccount:
    name: str
    kind: CashAccountKind
    bank_name: str | None = None
    iban: str | None = None
    opening_balance: Decimal = ZERO
    opening_date: date | None = None
    note: str | None = None


async def create_account(
    session: AsyncSession, data: NewAccount, *, today: date, created_by: str | None
) -> CashAccount:
    name = clean_account_name(data.name)
    taken = {n.casefold() for n in await session.scalars(select(CashAccount.name))}
    if name.casefold() in taken:
        raise FinanceRuleError(
            "cash_account_exists", f"'{name}' adında bir hesap zaten var.", conflict=True
        )
    opening_day = data.opening_date or today
    check_not_future(opening_day, today, "opening_date")
    order = await session.scalar(select(func.coalesce(func.max(CashAccount.sort_order), -1)))
    account = CashAccount(
        id=uuid.uuid7(),
        name=name,
        kind=data.kind.value,
        bank_name=clip(data.bank_name, 100),
        iban=clean_iban(data.iban),
        opening_balance=data.opening_balance,
        opening_date=data.opening_date,
        note=clip(data.note, NOTE_MAX),
        sort_order=(order or 0) + 1,
    )
    session.add(account)
    await session.flush()
    await lock_accounts(session, [account.id])
    amounts = opening_amounts(data.opening_balance)
    if amounts is not None:  # açılış da bir harekettir: ekstrede nereden geldiği görünsün
        await add_movement(
            session, account, day=opening_day, inflow=amounts[0], outflow=amounts[1],
            description="Açılış bakiyesi", source=CashSource.OPENING, created_by=created_by,
            lock=False,
        )  # fmt: skip
    return account


async def ensure_default_accounts(session: AsyncSession, *, iban: str | None) -> None:
    """Yeni site: "Banka Hesabı" (sitenin IBAN'ı) ve "Kasa" (docs/04 §10.1, docs/10 §3)."""
    existing = set(await session.scalars(select(CashAccount.name)))
    for order, (name, kind) in enumerate(DEFAULT_ACCOUNTS):
        if name in existing:
            continue
        session.add(
            CashAccount(
                name=name,
                kind=kind.value,
                iban=iban if kind is CashAccountKind.BANK else None,
                sort_order=order,
            )
        )
    await session.flush()


@dataclass(frozen=True, slots=True)
class AccountWithBalance:
    account: CashAccount
    inflow: Decimal
    outflow: Decimal
    balance: Decimal


async def accounts_with_balances(session: AsyncSession) -> list[AccountWithBalance]:
    rows = await session.execute(
        select(CashAccount, CashBalance)
        .outerjoin(
            CashBalance,
            and_(
                CashBalance.cash_account_id == CashAccount.id,
                CashBalance.site_id == CashAccount.site_id,
            ),
        )
        .order_by(CashAccount.sort_order, CashAccount.name)
    )
    zero = Decimal("0.00")
    return [
        AccountWithBalance(
            account,
            balance.inflow_total if balance else zero,
            balance.outflow_total if balance else zero,
            balance.balance if balance else zero,
        )
        for account, balance in rows
    ]


async def total_balance(session: AsyncSession) -> Decimal:
    """Toplam bakiye yalnız **aktif** hesapların toplamıdır (§10.2)."""
    value = await session.scalar(
        select(func.coalesce(func.sum(CashBalance.balance), 0))
        .join(
            CashAccount,
            and_(
                CashAccount.id == CashBalance.cash_account_id,
                CashAccount.site_id == CashBalance.site_id,
            ),
        )
        .where(CashAccount.is_active)
    )
    return Decimal(value or 0).quantize(Decimal("0.01"))


# --- Elle hareket, aktarım, geri alma (§10.3–10.4) --------------------------------


async def manual_movement(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    day: date,
    direction: Direction,
    amount: Decimal,
    description: str,
    reference: str | None,
    today: date,
    created_by: str,
) -> CashMovement:
    text_value = clean_text(description, "description")
    check_positive(amount)
    check_not_future(day, today)
    account = await active_account(session, account_id)
    inflow, outflow = (amount, ZERO) if direction is Direction.IN else (ZERO, amount)
    return await add_movement(
        session, account, day=day, inflow=inflow, outflow=outflow, description=text_value,
        source=CashSource.MANUAL, reference=clip(reference, REFERENCE_MAX), created_by=created_by,
    )  # fmt: skip


async def transfer(
    session: AsyncSession,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    day: date,
    amount: Decimal,
    note: str | None,
    today: date,
    created_by: str,
) -> tuple[CashMovement, CashMovement]:
    """İki hareket: gönderende çıkış, alanda giriş; birbirinin kimliğini taşır. Toplam değişmez."""
    if from_id == to_id:
        raise FinanceRuleError("same_account", "Gönderen ve alan hesap aynı olamaz.", field="to_id")
    check_positive(amount)
    check_not_future(day, today)
    source = await active_account(session, from_id)
    target = await active_account(session, to_id)
    label = clip(note, 200) or "Hesaplar arası aktarım"
    await lock_accounts(session, [source.id, target.id])
    out_id, in_id = uuid.uuid7(), uuid.uuid7()
    out = CashMovement(
        id=out_id, cash_account_id=source.id, date=day, outflow=amount, inflow=ZERO,
        description=f"{label} → {target.name}", source=CashSource.TRANSFER.value,
        source_id=in_id, created_by_name=created_by,
    )  # fmt: skip
    incoming = CashMovement(
        id=in_id, cash_account_id=target.id, date=day, inflow=amount, outflow=ZERO,
        description=f"{label} ← {source.name}", source=CashSource.TRANSFER.value,
        source_id=out_id, created_by_name=created_by,
    )  # fmt: skip
    session.add_all([out, incoming])
    await session.flush()
    site_id = _site_id()
    for account_id, inflow, outflow in ((source.id, ZERO, amount), (target.id, amount, ZERO)):
        await session.execute(
            _APPLY,
            {"site_id": site_id, "account_id": account_id, "inflow": inflow, "outflow": outflow},
        )
    return out, incoming


async def get_movement(session: AsyncSession, movement_id: uuid.UUID) -> CashMovement | None:
    movement: CashMovement | None = await session.scalar(
        select(CashMovement).where(CashMovement.id == movement_id)
    )
    return movement


async def reverse_movement(
    session: AsyncSession, movement: CashMovement, *, reason: str, today: date, created_by: str
) -> CashMovement:
    """Giriş/çıkış yer değiştirmiş hareket (§10.4). Kaynak kayıtlı hareket burada geri alınamaz."""
    account = await get_account(session, movement.cash_account_id)
    if account is None:  # pragma: no cover - FK
        raise FinanceRuleError("cash_account_not_found", "Kasa/banka hesabı bulunamadı.")
    await lock_accounts(session, [account.id])
    already = await session.scalar(
        select(exists().where(CashMovement.reversal_of_id == movement.id))
    )
    text_reason = check_movement_reversible(
        MovementState(
            CashSource(movement.source), movement.reversal_of_id is not None, bool(already)
        ),
        reason,
    )
    if not account.is_active:
        raise inactive_account_error()
    return await add_movement(
        session, account, day=today, inflow=movement.outflow, outflow=movement.inflow,
        description=f"DÜZELTME — {movement.description}", source=CashSource(movement.source),
        source_id=movement.source_id, reference=text_reason, reversal_of_id=movement.id,
        created_by=created_by, lock=False,
    )  # fmt: skip


# --- Ekstre (§10.5) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementLine:
    movement: CashMovement
    running_balance: Decimal
    is_reversed: bool


@dataclass(frozen=True, slots=True)
class Statement:
    opening: Decimal  # devreden: başlangıçtan önceki hareketlerin neti
    closing: Decimal
    total_in: Decimal
    total_out: Decimal
    count: int
    lines: list[StatementLine]


def _range(
    account_id: uuid.UUID, date_from: date | None, date_to: date | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [CashMovement.cash_account_id == account_id]
    if date_from is not None:
        conditions.append(CashMovement.date >= date_from)
    if date_to is not None:
        conditions.append(CashMovement.date <= date_to)
    return conditions


async def statement(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    date_from: date | None,
    date_to: date | None,
    offset: int,
    limit: int | None,
) -> Statement:
    """Yürüyen bakiye tüm aralık üzerinden, devredenden başlayarak (sayfa değişince bozulmaz)."""
    opening = ZERO
    if date_from is not None:
        opening = (
            await session.scalar(
                select(
                    func.coalesce(func.sum(CashMovement.inflow - CashMovement.outflow), 0)
                ).where(CashMovement.cash_account_id == account_id, CashMovement.date < date_from)
            )
            or ZERO
        )
    conditions = _range(account_id, date_from, date_to)
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(CashMovement.inflow), 0),
                func.coalesce(func.sum(CashMovement.outflow), 0),
                func.count(),
            ).where(*conditions)
        )
    ).one()
    total_in, total_out, count = Decimal(totals[0]), Decimal(totals[1]), totals[2]
    order = (CashMovement.date, CashMovement.created_at, CashMovement.id)
    running = (func.sum(CashMovement.inflow - CashMovement.outflow).over(order_by=order)).label(
        "running"
    )
    inner = select(CashMovement.id.label("movement_id"), running).where(*conditions).subquery()
    reversed_ids = select(CashMovement.reversal_of_id).where(
        CashMovement.reversal_of_id.is_not(None)
    )
    query: Select[*tuple[CashMovement, Decimal, bool]] = (
        select(CashMovement, inner.c.running, CashMovement.id.in_(reversed_ids))
        .join(inner, inner.c.movement_id == CashMovement.id)
        .order_by(CashMovement.date.desc(), CashMovement.created_at.desc(), CashMovement.id.desc())
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    lines = [
        StatementLine(movement, Decimal(opening) + value, bool(flag))
        for movement, value, flag in await session.execute(query)
    ]
    opening_value = Decimal(opening).quantize(Decimal("0.01"))
    return Statement(
        opening=opening_value,
        closing=opening_value + total_in - total_out,
        total_in=total_in,
        total_out=total_out,
        count=count,
        lines=lines,
    )
