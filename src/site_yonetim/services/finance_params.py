"""Finans parametreleri (FIN-16, servis isteği 33) — versiyonlu. Açık site kapsamında.

Bir dönemde geçerli değer = yürürlüğü (`effective_year/month`) o döneme kadar başlamış en son
**aktif** sürüm; hiç sürüm yoksa varsayılanlar (`domain/finance_params.DEFAULTS`). Finansal etkili
değişiklik en erken sonraki dönemden geçerli olur ve "Finans parametresi" onay kuralı varsa
onaya gider (202); onaylanınca aktifleşir. Onay, yürürlük dönemi başladıktan sonra gelirse sürüm
onay ayından sonraki döneme kayar — geçmiş dönem hiçbir zaman değişmez.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance_params import (
    DEFAULTS,
    FINANCIAL,
    changed_keys,
    check_effective,
    merge,
)
from site_yonetim.domain.fiscal import period_key, period_label
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import FinanceParameterVersion, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ

ACTIVE, PENDING, REJECTED = "active", "approval_pending", "rejected"
VERSIONS_SHOWN = 100


def _ym(v: FinanceParameterVersion) -> YearMonth:
    return YearMonth(v.effective_year, v.effective_month)


async def version_at(session: AsyncSession, period: YearMonth) -> FinanceParameterVersion | None:
    found: FinanceParameterVersion | None = await session.scalar(
        select(FinanceParameterVersion)
        .where(
            FinanceParameterVersion.status == ACTIVE,
            tuple_(FinanceParameterVersion.effective_year, FinanceParameterVersion.effective_month)
            <= tuple_(period.year, period.month),
        )
        .order_by(
            FinanceParameterVersion.effective_year.desc(),
            FinanceParameterVersion.effective_month.desc(),
            FinanceParameterVersion.created_at.desc(),
        )
        .limit(1)
    )
    return found


async def params_at(session: AsyncSession, period: YearMonth) -> dict[str, Any]:
    """O dönemde geçerli parametreler (eksik anahtar varsayılandan)."""
    version = await version_at(session, period)
    return {**DEFAULTS, **(version.values if version else {})}


async def current(session: AsyncSession, today: date) -> dict[str, Any]:
    return await params_at(session, YearMonth.of(today))


async def versions(session: AsyncSession) -> list[FinanceParameterVersion]:
    """Son sürümler (yeniden eskiye; reddedilenler dahil, ekranda geçmiş)."""
    return list(
        await session.scalars(
            select(FinanceParameterVersion)
            .order_by(FinanceParameterVersion.created_at.desc())
            .limit(VERSIONS_SHOWN)
        )
    )


@dataclass(frozen=True, slots=True)
class Saved:
    version: FinanceParameterVersion
    request: ApprovalRequest | None


async def save(
    session: AsyncSession,
    site: Site,
    *,
    effective: YearMonth | None,
    changes: dict[str, Any],
    actor: Person,
    today: date,
    now: datetime,
) -> Saved:
    """Yeni sürüm: değişen alanlar, yürürlük döneminde geçerli değerlerin üzerine yazılır.
    Hatalar: `FormFieldError` (422) — `effective_period` erken/geçmiş, değişiklik yok."""
    this_month = YearMonth.of(today)
    if effective is None:
        raise FormFieldError({"effective_period": "Yürürlük dönemini seçin (YYYY-AA)."})
    base = await params_at(session, effective)
    values, errors = merge(base, changes)
    if errors:
        raise FormFieldError(errors)
    changed = changed_keys(base, values)
    if not changed:
        raise NoChangeError
    timing = check_effective(effective, this_month, changed)
    if timing.get("effective_period") == "financial":
        raise EffectiveTooEarlyError(this_month.next())
    if timing:
        raise FormFieldError({"effective_period": "Geçmiş döneme parametre yazılamaz."})
    financial = any(k in FINANCIAL for k in changed)
    needs_approval = financial and (
        await approvals_svc.match_rule(
            session, ActionType.FINANCE_PARAMETER, amount=None, source=None, today=today
        )
        is not None
    )
    version = FinanceParameterVersion(
        id=uuid.uuid7(),
        effective_year=effective.year,
        effective_month=effective.month,
        values=values,
        changed=changed,
        status=PENDING if needs_approval else ACTIVE,
        changed_by_id=actor.user_id,
        changed_by=actor.name,
    )
    session.add(version)
    await session.flush()
    request = None
    if needs_approval:
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.FINANCE_PARAMETER,
                title=f"Finans parametresi — {period_label(effective)} döneminden",
                summary={
                    "effective_period": period_key(effective),
                    "changed": changed,
                    "before": {k: base.get(k) for k in changed},
                    "after": {k: values[k] for k in changed},
                },
                payload={"version_id": str(version.id)},
                subject_type="finance_parameter_versions",
                subject_id=version.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        version.approval_request_id = request.id
        await session.flush()
    return Saved(version, request)


class NoChangeError(Exception):
    """422 `no_change`."""


class EffectiveTooEarlyError(Exception):
    """422 `period_locked`: finansal etkili değişiklik en erken sonraki dönemden."""

    def __init__(self, earliest: YearMonth) -> None:
        super().__init__(period_label(earliest))
        self.earliest = earliest


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    version = await session.get(
        FinanceParameterVersion, uuid.UUID((request.payload or {})["version_id"])
    )
    if version is None or version.status != PENDING:
        raise ExecutionError("Parametre sürümü artık onay beklemiyor.")
    if request.decided_at is not None:
        decided = YearMonth.of(request.decided_at.astimezone(BUSINESS_TZ).date())
        if _ym(version) <= decided:  # yürürlük başladıysa geçmiş değişmez: sonraki döneme kayar
            target = decided.next()
            version.effective_year, version.effective_month = target.year, target.month
    version.status = ACTIVE
    await session.flush()


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    version = await session.get(
        FinanceParameterVersion, uuid.UUID((request.payload or {})["version_id"])
    )
    if version is not None and version.status == PENDING:
        version.status = REJECTED
        await session.flush()


approvals_svc.register_executor(ActionType.FINANCE_PARAMETER, _execute)
approvals_svc.register_closer(ActionType.FINANCE_PARAMETER, _close)
