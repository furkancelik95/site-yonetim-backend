"""Tadilat ve taşınma izinleri — Yazılım Dokümanı HIZ-05; servis isteği 52. Açık site
kapsamında; commit çağırana ait.

- Sakin kendi bölümü için başvurur (tarih bugünden önce olamaz; tadilatta bitiş ≥ başlangıç).
- Onay "Tadilat / taşınma izni" onay kuralından geçer (HIZ-05 adım 2, K21): kural varsa onay
  talebi açılır (202), onaylanınca izin yürütücüyle onaylanır; kural yoksa yetkili doğrudan onaylar.
- Onayda sakine, **komşu bölümlere** (aynı blokta aynı/bir alt/bir üst kat; kat bilinmiyorsa aynı
  blok) ve güvenliğe bilgi gider. Tadilatta depozito > 0 ise malik hesabına borç (kaynak
  `deposit`); iadesi tamamlamada **ters kayıtla**.
- Tamamlamada kontrol listesi zorunlu (giriş taşınması hariç). Ret gerekçesi zorunlu.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType, ApprovalStatus
from site_yonetim.domain.finance import LedgerSource
from site_yonetim.domain.money import round_money
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.service_desk import (
    DEFAULT_PERMIT_CHECKLIST,
    PERMIT_TYPE_LABELS,
    PermitStatus,
    PermitType,
    RuleError,
    check_hours_range,
    check_permit,
)
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.domain.text import format_date_tr, format_money_tr
from site_yonetim.models import (
    LedgerAccount,
    LedgerEntry,
    Permit,
    PermitSettings,
    Site,
    Unit,
    UnitParty,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import ledger, service_desk
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.charging import BUSINESS_TZ

CONTRACTOR_MAX, DESCRIPTION_MAX, REASON_MAX, CHECKLIST_MAX = 80, 500, 500, 30


# --- Kurallar ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rules:
    weekday_hours: str
    saturday_hours: str | None
    sunday_allowed: bool
    deposit_amount: Decimal
    checklist: list[str]


DEFAULT_RULES = Rules("09:00-18:00", "10:00-16:00", False, Decimal("0.00"),
                      list(DEFAULT_PERMIT_CHECKLIST))  # fmt: skip


async def rules(session: AsyncSession) -> Rules:
    row = await session.scalar(select(PermitSettings))
    if row is None:
        return DEFAULT_RULES
    return Rules(
        row.weekday_hours, row.saturday_hours, row.sunday_allowed, row.deposit_amount,
        list(row.checklist),
    )  # fmt: skip


async def save_rules(session: AsyncSession, data: Rules) -> Rules:
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    for field_name, raw, optional in (
        ("weekday_hours", data.weekday_hours, False),
        ("saturday_hours", data.saturday_hours, True),
    ):
        try:
            values[field_name] = check_hours_range(raw, field_name, optional=optional)
        except RuleError as exc:
            errors.update(exc.fields)
    if data.deposit_amount < 0:
        errors["deposit_amount"] = "Depozito negatif olamaz."
    checklist = [" ".join(x.split())[:120] for x in data.checklist if x and x.strip()]
    if len(checklist) > CHECKLIST_MAX:
        errors["checklist"] = f"En fazla {CHECKLIST_MAX} madde."
    if errors:
        raise RuleError(errors)
    row = await session.scalar(select(PermitSettings))
    if row is None:
        row = PermitSettings()
        session.add(row)
    row.weekday_hours = values["weekday_hours"]
    row.saturday_hours = values["saturday_hours"]
    row.sunday_allowed = data.sunday_allowed
    row.deposit_amount = round_money(data.deposit_amount)
    row.checklist = checklist
    await session.flush()
    return await rules(session)


def hours_text(r: Rules) -> str:
    return (
        f"hafta içi {r.weekday_hours}, cumartesi {r.saturday_hours or 'yasak'}, "
        f"pazar {'serbest' if r.sunday_allowed else 'yasak'}"
    )


# --- Başvuru -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermitData:
    unit_id: uuid.UUID
    type: PermitType
    date_from: date
    date_to: date | None
    contractor: str | None
    description: str | None


async def apply(
    session: AsyncSession, data: PermitData, *, person_id: uuid.UUID, actor: str, today: date
) -> Permit:
    """Sakin başvurusu; bölümün sakinin bölümü olduğu çağıranda denetlenir."""
    end, description = check_permit(
        data.type, data.date_from, data.date_to, data.description, today
    )
    permit = Permit(
        unit_id=data.unit_id,
        person_id=person_id,
        type=data.type.value,
        date_from=data.date_from,
        date_to=end,
        contractor=" ".join((data.contractor or "").split())[:CONTRACTOR_MAX] or None,
        description=description[:DESCRIPTION_MAX],
        status=PermitStatus.PENDING.value,
        checklist=[],
        created_by_name=actor,
    )
    session.add(permit)
    await session.flush()
    return permit


def list_query(*, status: PermitStatus | None, unit_ids: set[uuid.UUID] | None) -> Select[Permit]:
    query = select(Permit)
    if status is not None:
        query = query.where(Permit.status == status.value)
    if unit_ids is not None:
        query = query.where(Permit.unit_id.in_(unit_ids))
    return query.order_by(Permit.created_at.desc(), Permit.id.desc())


async def get(session: AsyncSession, permit_id: uuid.UUID) -> Permit | None:
    found: Permit | None = await session.scalar(
        select(Permit).where(Permit.id == permit_id).with_for_update()
    )
    return found


async def approval_status(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ApprovalStatus]:
    if not ids:
        return {}
    rows = await session.execute(
        select(ApprovalRequest.id, ApprovalRequest.status).where(ApprovalRequest.id.in_(ids))
    )
    return {i: ApprovalStatus(s) for i, s in rows}


def _pending(permit: Permit) -> None:
    if permit.status != PermitStatus.PENDING.value:
        raise OperationRuleError(
            "invalid_status", "Yalnız onay bekleyen başvuru için karar verilir.", conflict=True
        )


# --- Onay ----------------------------------------------------------------------------------


async def _deposit_account(session: AsyncSession, unit_id: uuid.UUID) -> LedgerAccount | None:
    accounts = list(
        await session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.unit_id == unit_id, LedgerAccount.is_closed.is_(False))
            .order_by(LedgerAccount.reference_code)
        )
    )
    owner = [a for a in accounts if a.kind == AccountKind.OWNER.value]
    return next(iter(owner or accounts), None)


async def _neighbour_users(
    session: AsyncSession, unit_id: uuid.UUID, today: date
) -> set[uuid.UUID]:
    unit = await session.get(Unit, unit_id)
    if unit is None:  # pragma: no cover
        return set()
    near = select(Unit.id).where(Unit.block_id == unit.block_id, Unit.id != unit.id)
    if unit.floor is not None:
        near = near.where(Unit.floor.between(unit.floor - 1, unit.floor + 1))
    people = set(
        await session.scalars(
            select(UnitParty.person_id).where(
                UnitParty.unit_id.in_(near),
                UnitParty.role != PartyRole.PROXY.value,
                UnitParty.start_date <= today,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
            )
        )
    )
    return await requests_svc.person_user_ids(session, people)


async def _notify(
    session: AsyncSession, users: set[uuid.UUID], permit: Permit, title: str, body: str, key: str
) -> None:
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="permit.status",
            title=title,
            body=body,
            link_entity="permits",
            link_id=permit.id,
        ),
        dedupe_key=f"permit:{permit.id}:{key}",
    )


async def _approve(
    session: AsyncSession, site: Site, permit: Permit, *, actor: str, now: datetime, today: date
) -> None:
    """Onayın etkisi: durum, depozito, bilgilendirme (doğrudan ya da onay yürütücüsünden)."""
    permit.status = PermitStatus.APPROVED.value
    permit.decided_by_name, permit.decided_at = actor, now
    r = await rules(session)
    kind = PermitType(permit.type)
    if kind is PermitType.RENOVATION and r.deposit_amount > 0:
        account = await _deposit_account(session, permit.unit_id)
        if account is None:
            raise OperationRuleError(
                "no_account", "Bu bölümde açık cari hesap yok; depozito yazılamaz.", conflict=True
            )
        await ledger.lock_accounts(session, [account.id])
        entry = LedgerEntry(
            id=uuid.uuid7(), account_id=account.id, date=today, due_date=permit.date_from,
            debit=r.deposit_amount, source=LedgerSource.DEPOSIT.value, source_id=permit.id,
            description=f"Tadilat depozitosu ({format_date_tr(permit.date_from)})",
        )  # fmt: skip
        session.add(entry)
        await session.flush()
        await ledger.refresh_balances(session, [account.id])
        permit.deposit, permit.deposit_entry_id = r.deposit_amount, entry.id
    await session.flush()
    label = PERMIT_TYPE_LABELS[kind]
    when = format_date_tr(permit.date_from) + (
        f" – {format_date_tr(permit.date_to)}" if permit.date_to != permit.date_from else ""
    )
    owner_users = await requests_svc.person_user_ids(
        session, {permit.person_id} if permit.person_id else set()
    )
    body = f"{label}: {when}."
    if kind is PermitType.RENOVATION:
        body += f" Çalışma saatleri: {hours_text(r)}."
        if permit.deposit:
            body += f" Depozito {format_money_tr(permit.deposit)} hesabınıza yazıldı."
    await _notify(session, owner_users, permit, f"{label} izniniz onaylandı", body, "approved")
    if kind is PermitType.RENOVATION:
        neighbours = await _neighbour_users(session, permit.unit_id, today) - owner_users
        await _notify(
            session, neighbours, permit, "Komşu bölümde tadilat",
            f"{when} arasında tadilat yapılacak; çalışma saatleri {hours_text(r)}.", "neighbours",
        )  # fmt: skip
    security = await service_desk.staff_user_ids(session, site, Permission.SECURITY_VISITORS)
    await _notify(
        session, security, permit, f"{label} izni: {when}",
        f"{permit.contractor or 'Firma belirtilmedi'} — giriş izne bağlı kontrol edilir.",
        "security",
    )  # fmt: skip


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    if request.subject_id is None:  # pragma: no cover
        raise approvals_svc.ExecutionError("Başvuru bulunamadı.")
    permit = await get(session, request.subject_id)
    if permit is None or permit.status != PermitStatus.PENDING.value:
        raise approvals_svc.ExecutionError("Başvuru artık onay beklemiyor.")
    site = await session.get(Site, permit.site_id)
    if site is None:  # pragma: no cover
        raise approvals_svc.ExecutionError("Site bulunamadı.")
    now = request.decided_at or request.created_at
    try:
        await _approve(
            session, site, permit, actor=f"Onay talebi #{request.number}", now=now,
            today=now.astimezone(BUSINESS_TZ).date(),
        )  # fmt: skip
    except OperationRuleError as exc:
        raise approvals_svc.ExecutionError(exc.message) from exc


approvals_svc.register_executor(ActionType.RENOVATION_PERMIT, _execute)


async def approve(
    session: AsyncSession,
    site: Site,
    permit: Permit,
    *,
    requester: approvals_svc.Person,
    now: datetime,
    today: date,
) -> ApprovalRequest | None:
    """Onay kuralı varsa onay talebi açılır (döner); yoksa izin hemen onaylanır (None)."""
    _pending(permit)
    if permit.approval_request_id is not None:
        current = (await approval_status(session, [permit.approval_request_id])).get(
            permit.approval_request_id
        )
        if current in OPEN_STATUSES:
            raise OperationRuleError(
                "approval_pending", "Bu başvuru zaten onay sürecinde.", conflict=True
            )
    rule = await approvals_svc.match_rule(
        session, ActionType.RENOVATION_PERMIT, amount=None, source=permit.type, today=today
    )
    if rule is None:
        await _approve(session, site, permit, actor=requester.name, now=now, today=today)
        return None
    unit_name = (await requests_svc.unit_names(session, {permit.unit_id})).get(permit.unit_id, "")
    label = PERMIT_TYPE_LABELS[PermitType(permit.type)]
    approval = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.RENOVATION_PERMIT,
            title=f"{label} izni — {unit_name}",
            summary={
                "unit_name": unit_name,
                "type": permit.type,
                "date_from": permit.date_from.isoformat(),
                "date_to": permit.date_to.isoformat(),
                "contractor": permit.contractor,
                "description": permit.description,
            },
            source=permit.type,
            subject_type="permits",
            subject_id=permit.id,
        ),
        requester=requester,
        today=today,
        now=now,
    )
    permit.approval_request_id = approval.id
    await session.flush()
    return approval


async def reject(
    session: AsyncSession, permit: Permit, reason: str | None, *, actor: str, now: datetime
) -> Permit:
    _pending(permit)
    text = " ".join((reason or "").split())
    if len(text) < 5:
        raise RuleError({"reason": "Ret gerekçesi yazın."})
    permit.status = PermitStatus.REJECTED.value
    permit.reject_reason = text[:REASON_MAX]
    permit.decided_by_name, permit.decided_at = actor, now
    await session.flush()
    users = await requests_svc.person_user_ids(
        session, {permit.person_id} if permit.person_id else set()
    )
    await _notify(
        session, users, permit, f"{PERMIT_TYPE_LABELS[PermitType(permit.type)]} başvurunuz "
        "reddedildi", f"Gerekçe: {permit.reject_reason}", "rejected",
    )  # fmt: skip
    return permit


async def complete(
    session: AsyncSession,
    permit: Permit,
    checklist: list[tuple[str, bool]],
    refund_deposit: bool,
    *,
    actor: str,
    now: datetime,
    today: date,
) -> Permit:
    if permit.status != PermitStatus.APPROVED.value:
        raise OperationRuleError(
            "invalid_status", "Yalnız onaylı başvuru tamamlanır.", conflict=True
        )
    items = [(" ".join(i.split())[:120], ok) for i, ok in checklist if i and i.strip()]
    if PermitType(permit.type) is not PermitType.MOVE_IN and not items:
        raise RuleError({"checklist": "Kontrol listesini doldurun."})
    permit.checklist = [{"item": i, "ok": ok} for i, ok in items[:CHECKLIST_MAX]]
    permit.status = PermitStatus.COMPLETED.value
    permit.completed_by_name, permit.completed_at = actor, now
    if refund_deposit and permit.deposit_entry_id is not None and permit.refund_entry_id is None:
        original = await session.get(LedgerEntry, permit.deposit_entry_id)
        if original is None:  # pragma: no cover
            raise OperationRuleError("deposit_missing", "Depozito kaydı bulunamadı.", conflict=True)
        await ledger.lock_accounts(session, [original.account_id])
        refund = LedgerEntry(
            id=uuid.uuid7(), account_id=original.account_id, date=today,
            credit=original.debit, source=LedgerSource.DEPOSIT.value, source_id=permit.id,
            description=f"Tadilat depozitosu iadesi (ters kayıt) — {original.description}"[:200],
            reversal_of_entry_id=original.id,
        )  # fmt: skip
        session.add(refund)
        await session.flush()
        await ledger.refresh_balances(session, [original.account_id])
        permit.refund_entry_id = refund.id
    await session.flush()
    return permit
