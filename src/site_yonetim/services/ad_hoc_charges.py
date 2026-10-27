"""Dönem dışı ücretler (FIN-06, servis isteği 31). Açık site kapsamında.

- Kaynak bazında yansıma ayarı (`immediate` / `next_period`, vade günü; varsayılan anında, 10 gün).
- `immediate`: bölümün seçilen cari hesabına **tek kalem borç** (`source = ad_hoc_charge`), kendi
  vadesiyle. `next_period`: bekleyen ek kalem; bir sonraki tahakkuk koşusunda aynı transaction'da
  "ek kalemler" başlığıyla yazılır (`post_waiting`); koşu ters kaydedilirse ters kaydı alınır ve
  ücret yeniden bekler.
- İptal silmez: yazılmış ücret ters kayıtla kapanır; bekleyen ücret tahakkuka eklenmez.
- Elle ücret "Dönem dışı ücret" onay kuralı varsa onay bekler (202).
- İç servis: `create()` rezervasyon, ücretli hizmet, tadilat modülleri gelince onlardan çağrılır.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.structure import AccountKind
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    AdHocCharge,
    AdHocChargeSettings,
    ChargeRun,
    LedgerAccount,
    LedgerEntry,
    Site,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.models.finance_ops import AD_HOC_SOURCES
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import ledger, period_lock
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ

SOURCE_LABELS = {
    "reservation": "Rezervasyon",
    "service": "Ücretli hizmet",
    "renovation": "Tadilat",
    "penalty": "Ceza",
    "other": "Diğer",
}
MODES = ("immediate", "next_period")
DEFAULT_SOURCE = {"mode": "immediate", "due_days": 10}
PENDING, POSTED, WAITING = "approval_pending", "posted", "waiting_next_run"
CANCELLED, REJECTED = "cancelled", "rejected"
DESCRIPTION_MAX = 200


# --- Ayar ---------------------------------------------------------------------------------


async def settings(session: AsyncSession) -> dict[str, dict[str, Any]]:
    row = await session.scalar(select(AdHocChargeSettings))
    stored = row.sources if row else {}
    return {s: {**DEFAULT_SOURCE, **stored.get(s, {})} for s in AD_HOC_SOURCES}


async def save_settings(
    session: AsyncSession, sources: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    current = await settings(session)
    errors: dict[str, str] = {}
    for key, value in sources.items():
        if key not in AD_HOC_SOURCES:
            errors[f"sources.{key}"] = "Bu kaynak tanınmıyor."
            continue
        mode, days = value.get("mode"), value.get("due_days")
        if (
            mode not in MODES
            or not isinstance(days, int)
            or isinstance(days, bool)
            or not (0 <= days <= 60)
        ):
            errors[f"sources.{key}"] = "Yansıma (anında / sonraki tahakkuk) ve 0–60 gün vade seçin."
            continue
        current[key] = {"mode": mode, "due_days": days}
    if errors:
        raise FormFieldError(errors)
    row = await session.scalar(select(AdHocChargeSettings))
    if row is None:
        session.add(AdHocChargeSettings(sources=current))
    else:
        row.sources = current
    await session.flush()
    return current


# --- Oluşturma ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewCharge:
    unit_id: uuid.UUID | None
    account_kind: str | None
    source: str | None
    description: str | None
    amount: Decimal | None
    due_date: date | None


@dataclass(frozen=True, slots=True)
class Created:
    charge: AdHocCharge
    request: ApprovalRequest | None


def _validated(data: NewCharge, today: date) -> tuple[uuid.UUID, AccountKind, str, str, Decimal]:
    errors: dict[str, str] = {}
    if data.unit_id is None:
        errors["unit_id"] = "Bölüm seçin."
    kind = (
        AccountKind(data.account_kind)
        if data.account_kind in {k.value for k in AccountKind}
        else None
    )
    if kind is None:
        errors["account_kind"] = "Kime yazılacağını seçin (malik / oturan)."
    if data.source not in AD_HOC_SOURCES:
        errors["source"] = "Kaynağı seçin."
    text = " ".join((data.description or "").split())[:DESCRIPTION_MAX]
    if len(text) < 3:
        errors["description"] = "Açıklama yazın (en az 3 karakter)."
    if data.amount is None or data.amount <= ZERO or data.amount != round_money(data.amount):
        errors["amount"] = "Tutar sıfırdan büyük olmalı."
    if data.due_date is not None and data.due_date < today:
        errors["due_date"] = "Vade bugünden önce olamaz."
    if errors or data.unit_id is None or kind is None or data.amount is None:
        raise FormFieldError(errors)
    return data.unit_id, kind, str(data.source), text, data.amount


async def create(
    session: AsyncSession,
    site: Site,
    data: NewCharge,
    *,
    actor: Person,
    today: date,
    now: datetime,
    manual: bool = True,
) -> Created:
    """Elle ücret (yönetici) ya da modül ücreti (`manual=False`: onay kuralına tabi değil)."""
    unit_id, kind, source, text, amount = _validated(data, today)
    account = await session.scalar(
        select(LedgerAccount)
        .where(
            LedgerAccount.unit_id == unit_id,
            LedgerAccount.kind == kind.value,
            LedgerAccount.is_closed.is_(False),
        )
        .order_by(LedgerAccount.created_at.desc())
        .limit(1)
    )
    if account is None:
        who = "malik" if kind is AccountKind.OWNER else "oturan"
        raise FinanceRuleError(
            "no_account", f"Bu bölümde açık {who} hesabı yok.", field="account_kind"
        )
    setting = (await settings(session))[source]
    await period_lock.ensure_writable(session, today)
    needs_approval = manual and (
        await approvals_svc.match_rule(
            session, ActionType.AD_HOC_CHARGE, amount=amount, source=source, today=today
        )
        is not None
    )
    charge = AdHocCharge(
        id=uuid.uuid7(),
        unit_id=unit_id,
        account_id=account.id,
        account_kind=kind.value,
        source=source,
        description=text,
        amount=amount,
        due_date=data.due_date,
        due_days=int(setting["due_days"]),
        mode=str(setting["mode"]),
        status=PENDING,
        created_by_id=actor.user_id,
        created_by=actor.name,
    )
    session.add(charge)
    await session.flush()
    if not needs_approval:
        await _apply(session, charge, today)
        return Created(charge, None)
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.AD_HOC_CHARGE,
            title=f"Dönem dışı ücret — {account.reference_code}, {format_money_tr(amount)}",
            summary={
                "reference_code": account.reference_code,
                "source": source,
                "description": text,
                "amount": f"{amount:.2f}",
                "mode": charge.mode,
            },
            payload={"charge_id": str(charge.id)},
            amount=amount,
            source=source,
            subject_type="ad_hoc_charges",
            subject_id=charge.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    charge.approval_request_id = request.id
    await session.flush()
    return Created(charge, request)


def _due(charge: AdHocCharge, day: date) -> date:
    return charge.due_date or day + timedelta(days=charge.due_days)


def _entry(charge: AdHocCharge, day: date, due: date, prefix: str = "") -> LedgerEntry:
    label = SOURCE_LABELS.get(charge.source, charge.source)
    return LedgerEntry(
        id=uuid.uuid7(),
        account_id=charge.account_id,
        date=day,
        due_date=due,
        debit=charge.amount,
        source=LedgerSource.AD_HOC_CHARGE.value,
        source_id=charge.id,
        description=f"{prefix}{label} — {charge.description}"[:DESCRIPTION_MAX],
    )


async def _apply(session: AsyncSession, charge: AdHocCharge, day: date) -> None:
    """Onaysız ya da onaylanınca: anında yazılır veya sonraki tahakkuku bekler."""
    if charge.mode == "next_period":
        charge.status = WAITING
        await session.flush()
        return
    await period_lock.ensure_writable(session, day)
    await ledger.lock_accounts(session, [charge.account_id])
    entry = _entry(charge, day, _due(charge, day))
    session.add(entry)
    charge.ledger_entry_id = entry.id
    charge.status = POSTED
    await session.flush()
    await ledger.refresh_balances(session, [charge.account_id])


# --- Tahakkukla birlikte ("ek kalemler") ------------------------------------------------


async def waiting(session: AsyncSession) -> list[AdHocCharge]:
    return list(
        await session.scalars(
            select(AdHocCharge)
            .where(AdHocCharge.status == WAITING)
            .order_by(AdHocCharge.created_at)
        )
    )


async def post_waiting(
    session: AsyncSession, run: ChargeRun, period: YearMonth
) -> tuple[list[uuid.UUID], Decimal]:
    """Koşu kaydedilirken bekleyen ücretleri "ek kalemler" olarak yazar (aynı transaction).
    Kapanmış hesabın ücreti bekler. Dönüş: (hesaplar, toplam)."""
    rows = list(await waiting(session))
    if not rows:
        return [], ZERO
    open_accounts = set(
        await session.scalars(
            select(LedgerAccount.id).where(
                LedgerAccount.id.in_({c.account_id for c in rows}),
                LedgerAccount.is_closed.is_(False),
            )
        )
    )
    posted = [c for c in rows if c.account_id in open_accounts]
    await ledger.lock_accounts(session, [c.account_id for c in posted])
    for charge in posted:
        entry = _entry(
            charge,
            run.charge_date,
            charge.due_date or run.due_date,
            prefix=f"{period.name} ek kalemler — ",
        )
        session.add(entry)
        charge.ledger_entry_id = entry.id
        charge.charge_run_id = run.id
        charge.status = POSTED
    await session.flush()
    accounts = [c.account_id for c in posted]
    await ledger.refresh_balances(session, accounts)
    return accounts, sum((c.amount for c in posted), ZERO)


async def reverse_for_run(session: AsyncSession, run: ChargeRun, today: date) -> list[uuid.UUID]:
    """Koşu ters kaydedilince ek kalemlerin de ters kaydı alınır; ücret yeniden bekler."""
    rows = list(
        await session.scalars(select(AdHocCharge).where(AdHocCharge.charge_run_id == run.id))
    )
    await ledger.lock_accounts(session, [c.account_id for c in rows])
    for charge in rows:
        if charge.status != POSTED or charge.ledger_entry_id is None:
            continue
        await _reverse_entry(session, charge, today, "TERS KAYIT — tahakkuk iptaliyle ek kalem")
        charge.status = WAITING
        charge.charge_run_id = None
        charge.ledger_entry_id = None
        charge.reversal_entry_id = None
    await session.flush()
    return [c.account_id for c in rows]


async def _reverse_entry(
    session: AsyncSession, charge: AdHocCharge, today: date, text: str
) -> None:
    original = await session.get(LedgerEntry, charge.ledger_entry_id)
    if original is None:  # pragma: no cover - FK
        raise FinanceRuleError("not_found", "Ücretin defter kaydı bulunamadı.")
    reversal = LedgerEntry(
        id=uuid.uuid7(),
        account_id=original.account_id,
        date=today,
        credit=original.debit,
        source=LedgerSource.AD_HOC_CHARGE.value,
        source_id=charge.id,
        description=f"{text}: {charge.description}"[:DESCRIPTION_MAX],
        reversal_of_entry_id=original.id,
    )
    session.add(reversal)
    charge.reversal_entry_id = reversal.id


# --- İptal --------------------------------------------------------------------------------


async def get(session: AsyncSession, charge_id: uuid.UUID) -> AdHocCharge | None:
    found: AdHocCharge | None = await session.scalar(
        select(AdHocCharge).where(AdHocCharge.id == charge_id).with_for_update()
    )
    return found


async def cancel(
    session: AsyncSession, charge: AdHocCharge, *, reason: str | None, today: date, now: datetime
) -> AdHocCharge:
    text = " ".join((reason or "").split())[:500]
    if len(text) < 5:
        raise FinanceRuleError("validation", "Gerekçe yazın (en az 5 karakter).", field="reason")
    if charge.status in (CANCELLED, REJECTED):
        raise FinanceRuleError("already_cancelled", "Ücret zaten iptal edilmiş.", conflict=True)
    if charge.status == PENDING:
        raise FinanceRuleError(
            "charge_pending",
            "Onay bekleyen ücret iptal edilmez; talebi onay kutusundan geri çekin.",
            conflict=True,
        )
    if charge.status == POSTED and charge.ledger_entry_id is not None:
        original = await session.get(LedgerEntry, charge.ledger_entry_id)
        if original is not None:
            await period_lock.ensure_writable(session, original.date, original=True)
        await period_lock.ensure_writable(session, today)
        await ledger.lock_accounts(session, [charge.account_id])
        await _reverse_entry(session, charge, today, "TERS KAYIT — ücret iptali")
        await session.flush()
        await ledger.refresh_balances(session, [charge.account_id])
    charge.status = CANCELLED
    charge.cancelled_at = now
    charge.cancel_reason = text
    await session.flush()
    return charge


# --- Onay ---------------------------------------------------------------------------------


async def _charge_of(session: AsyncSession, request: ApprovalRequest) -> AdHocCharge | None:
    return await session.get(AdHocCharge, uuid.UUID((request.payload or {})["charge_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    charge = await _charge_of(session, request)
    if charge is None or charge.status != PENDING:
        raise ExecutionError("Ücret artık onay beklemiyor.")
    decided = (request.decided_at or datetime.now().astimezone()).astimezone(BUSINESS_TZ)
    try:
        await _apply(session, charge, decided.date())
    except FinanceRuleError as exc:
        raise ExecutionError(exc.message) from exc


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    charge = await _charge_of(session, request)
    if charge is not None and charge.status == PENDING:
        charge.status = REJECTED
        await session.flush()


approvals_svc.register_executor(ActionType.AD_HOC_CHARGE, _execute)
approvals_svc.register_closer(ActionType.AD_HOC_CHARGE, _close)
