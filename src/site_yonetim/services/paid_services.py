"""Ücretli hizmet — tarife, teklif, sakin onayı (Yazılım Dokümanı HIZ-04, §12.2; servis isteği 51).
Açık site kapsamında; commit çağırana ait.

- Teklif yalnız bölüm içi talepte (409 `not_unit_request`); talepte tek açık teklif (409
  `quote_open`; veritabanında kısmi benzersiz indeks). Ücret yazılacak cari hesap teklif anında
  belirlenir: bölümün açık **oturan** hesabı, yoksa açık herhangi bir hesabı.
- **Sakin onayı olmadan ücret yansımaz.** Ret → iş ücretli yapılmaz.
- Tamamlamada gerçekleşen ≤ teklif → gerçekleşen faturalanır; aşarsa ek tutar sakin onayına gider
  (onay → gerçekleşen, ret → teklif tutarı).
- Faturalama: cari deftere borç (kaynak `service`), vade tahakkuktaki gibi +14 gün. Dönem dışı
  ücret modülü (servis isteği 31) henüz backend'de değil; o gelince yansıma kuralı oradan okunur.
- Tarife ekleme/değişikliği "Ücretli hizmet tarifesi" onay kuralından geçer (kural varsa 202).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.finance import LedgerSource
from site_yonetim.domain.money import round_money
from site_yonetim.domain.operations import (
    FINAL_STATUSES,
    OperationRuleError,
    RequestEventKind,
    RequestStatus,
)
from site_yonetim.domain.service_desk import (
    OPEN_QUOTE_STATUSES,
    QuoteLine,
    QuoteStatus,
    RuleError,
    billed_amount,
    quote_lines,
    quote_total,
)
from site_yonetim.domain.structure import AccountKind
from site_yonetim.domain.text import ascii_fold, format_money_tr
from site_yonetim.models import (
    LedgerAccount,
    LedgerEntry,
    Request,
    ServiceQuote,
    ServiceQuoteLine,
    ServiceTariff,
    Site,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import ledger, period_lock
from site_yonetim.services import requests as requests_svc

NAME_MAX, UNIT_MAX, NOTE_MAX = 60, 20, 200
# Ücretli hizmet borcunun vadesi (aidat vadesi K25 ile değişti; bu sabit kalır — dönem dışı ücret
# ayarına taşınınca oradan okunacak).
DUE_AFTER_DAYS = 14


# --- Tarife ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TariffData:
    name: str | None
    unit: str | None
    price: Decimal | None
    is_active: bool = True


def _tariff_key(name: str) -> str:
    return " ".join(ascii_fold(name).split())


async def tariffs(session: AsyncSession) -> list[ServiceTariff]:
    return list(
        await session.scalars(
            select(ServiceTariff).order_by(ServiceTariff.is_active.desc(), ServiceTariff.name)
        )
    )


async def _checked_tariff(
    session: AsyncSession, data: TariffData, tariff_id: uuid.UUID | None
) -> tuple[str, str, Decimal]:
    errors: dict[str, str] = {}
    name = " ".join((data.name or "").split())
    unit = " ".join((data.unit or "").split())
    if not 2 <= len(name) <= NAME_MAX:
        errors["name"] = "Kalem adını yazın."
    if not 1 <= len(unit) <= UNIT_MAX:
        errors["unit"] = "Birim yazın (saat, adet, iş)."
    if data.price is None or data.price <= 0:
        errors["price"] = "Birim fiyat sıfırdan büyük olmalı."
    if not errors:
        clash = await session.scalar(
            select(ServiceTariff.id).where(
                ServiceTariff.name_key == _tariff_key(name),
                ServiceTariff.id != (tariff_id or uuid.UUID(int=0)),
            )
        )
        if clash is not None:
            errors["name"] = "Bu adla bir tarife kalemi zaten var."
    if errors:
        raise RuleError(errors)
    price = cast(Decimal, data.price)  # yukarıda doğrulandı
    return name, unit, round_money(price)


async def _write_tariff(session: AsyncSession, payload: dict[str, Any]) -> ServiceTariff:
    tariff: ServiceTariff | None = None
    if payload.get("tariff_id"):
        tariff = await session.get(ServiceTariff, uuid.UUID(payload["tariff_id"]))
        if tariff is None:
            raise approvals_svc.ExecutionError("Tarife kalemi artık yok.")
    data = TariffData(payload["name"], payload["unit"], Decimal(payload["price"]),
                      bool(payload.get("is_active", True)))  # fmt: skip
    name, unit, price = await _checked_tariff(session, data, tariff.id if tariff else None)
    if tariff is None:
        tariff = ServiceTariff()
        session.add(tariff)
    tariff.name, tariff.name_key, tariff.unit = name, _tariff_key(name), unit
    tariff.price, tariff.is_active = price, data.is_active
    await session.flush()
    return tariff


async def _execute_tariff(session: AsyncSession, request: ApprovalRequest) -> None:
    try:
        await _write_tariff(session, request.payload or {})
    except RuleError as exc:
        raise approvals_svc.ExecutionError("; ".join(exc.fields.values())) from exc


approvals_svc.register_executor(ActionType.PAID_SERVICE_TARIFF, _execute_tariff)


@dataclass(frozen=True, slots=True)
class TariffWrite:
    tariff: ServiceTariff | None
    approval: ApprovalRequest | None


async def save_tariff(
    session: AsyncSession,
    site: Site,
    data: TariffData,
    *,
    tariff: ServiceTariff | None,
    requester: approvals_svc.Person,
    today: date,
    now: datetime,
) -> TariffWrite:
    name, unit, price = await _checked_tariff(session, data, tariff.id if tariff else None)
    payload = {
        "tariff_id": str(tariff.id) if tariff else None,
        "name": name,
        "unit": unit,
        "price": str(price),
        "is_active": data.is_active,
    }
    rule = await approvals_svc.match_rule(
        session, ActionType.PAID_SERVICE_TARIFF, amount=price, source=None, today=today
    )
    if rule is None:
        return TariffWrite(await _write_tariff(session, payload), None)
    approval = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.PAID_SERVICE_TARIFF,
            title=f"Hizmet tarifesi {'değişikliği' if tariff else 'ekleme'} — {name}",
            summary={
                "name": name,
                "unit": unit,
                "price": str(price),
                "previous_price": str(tariff.price) if tariff else None,
                "is_active": data.is_active,
            },
            payload=payload,
            amount=price,
            subject_type="service_tariffs",
            subject_id=tariff.id if tariff else None,
        ),
        requester=requester,
        today=today,
        now=now,
    )
    return TariffWrite(None, approval)


# --- Teklif ----------------------------------------------------------------------------------


async def billing_account(session: AsyncSession, unit_id: uuid.UUID) -> LedgerAccount | None:
    accounts = list(
        await session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.unit_id == unit_id, LedgerAccount.is_closed.is_(False))
            .order_by(LedgerAccount.reference_code)
        )
    )
    occupant = [a for a in accounts if a.kind == AccountKind.OCCUPANT.value]
    return next(iter(occupant or accounts), None)


async def quote_for(
    session: AsyncSession, request_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ServiceQuote]:
    """Taleplerin son teklifi."""
    if not request_ids:
        return {}
    rows = await session.scalars(
        select(ServiceQuote)
        .where(ServiceQuote.request_id.in_(request_ids))
        .order_by(ServiceQuote.request_id, ServiceQuote.id.desc())
        .ext(distinct_on(ServiceQuote.request_id))
    )
    return {q.request_id: q for q in rows}


async def lines_of(
    session: AsyncSession, quote_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ServiceQuoteLine]]:
    out: dict[uuid.UUID, list[ServiceQuoteLine]] = {i: [] for i in quote_ids}
    if not quote_ids:
        return out
    for line in await session.scalars(
        select(ServiceQuoteLine)
        .where(ServiceQuoteLine.quote_id.in_(quote_ids))
        .order_by(ServiceQuoteLine.quote_id, ServiceQuoteLine.line_no)
    ):
        out[line.quote_id].append(line)
    return out


async def create_quote(
    session: AsyncSession,
    request: Request,
    raw_lines: list[tuple[str, object, object]],
    note: str | None,
    *,
    actor: str,
    now: datetime,
) -> ServiceQuote:
    """`request` kilitli gelmeli."""
    if request.unit_id is None:
        raise OperationRuleError(
            "not_unit_request", "Ücretli hizmet yalnız bölüm içi taleplerde verilir.",
            conflict=True,
        )  # fmt: skip
    if RequestStatus(request.status) in FINAL_STATUSES:
        raise OperationRuleError(
            "invalid_status", "Kapanmış ya da iptal edilmiş talebe teklif verilemez.",
            conflict=True,
        )  # fmt: skip
    current = (await quote_for(session, [request.id])).get(request.id)
    if current is not None and QuoteStatus(current.status) in OPEN_QUOTE_STATUSES:
        raise OperationRuleError("quote_open", "Bu talepte açık bir teklif var.", conflict=True)
    lines: list[QuoteLine] = quote_lines(raw_lines)
    account = await billing_account(session, request.unit_id)
    if account is None:
        raise OperationRuleError(
            "no_account", "Bu bölümde açık cari hesap yok; önce bölüme malik/kiracı ekleyin.",
            conflict=True,
        )  # fmt: skip
    quote = ServiceQuote(
        id=uuid.uuid7(),
        request_id=request.id,
        unit_id=request.unit_id,
        account_id=account.id,
        total=quote_total(lines),
        note=" ".join((note or "").split())[:NOTE_MAX] or None,
        status=QuoteStatus.PENDING.value,
        created_by_name=actor,
    )
    session.add(quote)
    await session.flush()
    session.add_all(
        ServiceQuoteLine(
            quote_id=quote.id, line_no=i, description=line.description, qty=line.qty,
            unit_price=line.unit_price, total=line.total,
        )
        for i, line in enumerate(lines, start=1)
    )  # fmt: skip
    session.add(
        requests_svc.event(
            request, RequestEventKind.QUOTE,
            f"Ücretli hizmet teklifi: {format_money_tr(quote.total)} — sakin onayı bekleniyor.",
            actor, at=now,
        )
    )  # fmt: skip
    await session.flush()
    await requests_svc.notify_reporter(
        session, request, f"Ücretli hizmet teklifi: {format_money_tr(quote.total)}",
        key=f"quote:{quote.id}", body=f"#{request.number} {request.title} — onayınız bekleniyor.",
    )  # fmt: skip
    return quote


async def get_quote(session: AsyncSession, quote_id: uuid.UUID) -> ServiceQuote | None:
    found: ServiceQuote | None = await session.scalar(
        select(ServiceQuote).where(ServiceQuote.id == quote_id).with_for_update()
    )
    return found


async def _bill(
    session: AsyncSession, quote: ServiceQuote, request: Request, amount: Decimal, today: date
) -> None:
    await period_lock.ensure_writable(session, today)
    await ledger.lock_accounts(session, [quote.account_id])
    entry = LedgerEntry(
        id=uuid.uuid7(),
        account_id=quote.account_id,
        date=today,
        due_date=today + timedelta(days=DUE_AFTER_DAYS),
        debit=amount,
        source=LedgerSource.SERVICE.value,
        source_id=quote.id,
        description=f"Ücretli hizmet — talep #{request.number} {request.title}"[:200],
    )
    session.add(entry)
    await session.flush()
    await ledger.refresh_balances(session, [quote.account_id])
    quote.status = QuoteStatus.BILLED.value
    quote.billed_total = amount
    quote.ledger_entry_id = entry.id


async def decide(
    session: AsyncSession,
    quote: ServiceQuote,
    request: Request,
    *,
    accept: bool,
    actor: str,
    now: datetime,
    today: date,
) -> ServiceQuote:
    """Sakin kararı: teklif (pending) ya da ek tutar (extra_pending)."""
    status = QuoteStatus(quote.status)
    if status is QuoteStatus.PENDING:
        quote.status = (QuoteStatus.ACCEPTED if accept else QuoteStatus.REJECTED).value
        quote.decided_at, quote.decided_by_name = now, actor
        text = "Sakin teklifi onayladı." if accept else (
            "Sakin teklifi reddetti; iş ücretli yapılmayacak."
        )  # fmt: skip
    elif status is QuoteStatus.EXTRA_PENDING:
        actual = quote.actual_total or quote.total
        amount = billed_amount(quote.total, actual, extra_accepted=accept)
        await _bill(session, quote, request, amount, today)
        text = (
            f"Sakin ek tutarı {'onayladı' if accept else 'reddetti'}; "
            f"{format_money_tr(amount)} cariye yansıdı."
        )
    else:
        raise OperationRuleError(
            "invalid_status", "Bu teklif için karar beklenmiyor.", conflict=True
        )
    session.add(requests_svc.event(request, RequestEventKind.QUOTE, text, actor, at=now))
    await session.flush()
    return quote


async def complete(
    session: AsyncSession,
    quote: ServiceQuote,
    request: Request,
    actual_total: Decimal | None,
    *,
    actor: str,
    now: datetime,
    today: date,
) -> ServiceQuote:
    if quote.status != QuoteStatus.ACCEPTED.value:
        raise OperationRuleError(
            "invalid_status", "Yalnız sakinin onayladığı teklif tamamlanır.", conflict=True
        )
    if actual_total is None or actual_total <= 0:
        raise RuleError({"actual_total": "Gerçekleşen tutarı girin."})
    actual = round_money(actual_total)
    quote.actual_total = actual
    quote.completed_at, quote.completed_by_name = now, actor
    if actual > quote.total:
        quote.status = QuoteStatus.EXTRA_PENDING.value
        text = (
            f"İş tamamlandı; gerçekleşen {format_money_tr(actual)} teklifi aşıyor — "
            "ek tutar için sakin onayı istendi."
        )
        await requests_svc.notify_reporter(
            session, request, f"Ek tutar onayı: {format_money_tr(actual)}",
            key=f"quote_extra:{quote.id}",
        )  # fmt: skip
    else:
        await _bill(session, quote, request, actual, today)
        text = f"İş tamamlandı; {format_money_tr(actual)} cariye yansıdı."
    session.add(requests_svc.event(request, RequestEventKind.QUOTE, text, actor, at=now))
    await session.flush()
    return quote
