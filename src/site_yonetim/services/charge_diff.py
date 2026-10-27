"""Tahakkuk fark raporu (FIN-05 taslak ekranı; servis isteği 34). Salt okuma, açık site kapsamı.

Önizlemedeki her hesap ↔ son kesinleşmiş (ters kaydı alınmamış) koşudaki aynı hesap:
`|değişim| ≥ eşik` → artış/azalış; önceki koşuda yok → yeni; bu dönemde yok → eksik. Eşik finans
parametresidir (`diff_threshold_percent`, varsayılan %10). Önceki koşu yoksa liste boştur.
Tek istekte, sabit sayıda sorguyla hesaplanır (3.000 bölümlük sitede de).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import ChargeRunStatus
from site_yonetim.domain.money import ZERO
from site_yonetim.models import Charge, ChargeRun, LedgerAccount, Period
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import finance_params
from site_yonetim.services.charging import PreparedRun


@dataclass(frozen=True, slots=True)
class DiffRow:
    account_id: uuid.UUID
    unit_name: str
    person_name: str
    account_kind: str
    previous: Decimal | None
    current: Decimal | None
    change_percent: Decimal | None
    flag: str  # increase | decrease | new | missing


@dataclass(frozen=True, slots=True)
class DiffReport:
    period: YearMonth
    base_period: YearMonth | None
    threshold: int
    compared: int
    rows: list[DiffRow]

    def counts(self) -> dict[str, int]:
        out = {"increase": 0, "decrease": 0, "new": 0, "missing": 0}
        for row in self.rows:
            out[row.flag] += 1
        return out


async def _base_run(session: AsyncSession, period: YearMonth) -> tuple[uuid.UUID, YearMonth] | None:
    """Önizlenen dönemden önceki son geçerli koşu (yoksa sonraki dönemlerden en yakını değil:
    karşılaştırma yalnız geçmişe)."""
    row = (
        await session.execute(
            select(ChargeRun.id, Period.year, Period.month)
            .join(
                Period, and_(Period.id == ChargeRun.period_id, Period.site_id == ChargeRun.site_id)
            )
            .where(
                ChargeRun.status == ChargeRunStatus.POSTED.value,
                ChargeRun.reversal_of_run_id.is_(None),
                tuple_(Period.year, Period.month) < tuple_(period.year, period.month),
            )
            .order_by(Period.year.desc(), Period.month.desc())
            .limit(1)
        )
    ).first()
    return (row.id, YearMonth(row.year, row.month)) if row else None


def _percent(previous: Decimal, current: Decimal) -> Decimal:
    if previous == ZERO:
        return Decimal(100)
    return ((current - previous) / previous * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


async def report(session: AsyncSession, prepared: PreparedRun) -> DiffReport:
    params = await finance_params.params_at(session, prepared.period)
    threshold = int(params["diff_threshold_percent"])
    current: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    names: dict[uuid.UUID, tuple[str, str, str]] = {}
    for charge in prepared.preview.charges:
        current[charge.ledger_account_id] += charge.amount
        names[charge.ledger_account_id] = (
            charge.unit_name,
            charge.person_name,
            charge.account_kind.value,
        )
    base = await _base_run(session, prepared.period)
    if base is None:
        return DiffReport(prepared.period, None, threshold, len(current), [])
    previous: dict[uuid.UUID, Decimal] = {}
    for account_id, total in await session.execute(
        select(Charge.ledger_account_id, func.sum(Charge.amount))
        .where(Charge.charge_run_id == base[0])
        .group_by(Charge.ledger_account_id)
    ):
        previous[account_id] = total
    missing_ids = [a for a in previous if a not in current]
    if missing_ids:
        for values in await session.execute(
            accounts_svc.accounts_query().where(LedgerAccount.id.in_(missing_ids))
        ):
            row = accounts_svc.to_row(tuple(values))
            names[row.account.id] = (row.unit_name, row.person_name, row.account.kind)
    rows: list[DiffRow] = []
    for account_id, amount in current.items():
        unit, person, kind = names[account_id]
        before = previous.get(account_id)
        if before is None:
            rows.append(DiffRow(account_id, unit, person, kind, None, amount, None, "new"))
            continue
        change = _percent(before, amount)
        if abs(change) >= threshold:
            flag = "increase" if amount > before else "decrease"
            rows.append(DiffRow(account_id, unit, person, kind, before, amount, change, flag))
    for account_id in missing_ids:
        unit, person, kind = names.get(account_id, ("—", "—", "occupant"))
        rows.append(
            DiffRow(account_id, unit, person, kind, previous[account_id], None, None, "missing")
        )
    rows.sort(key=lambda r: (r.unit_name, r.account_kind))
    return DiffReport(prepared.period, base[1], threshold, len(current), rows)
