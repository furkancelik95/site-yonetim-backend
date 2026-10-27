"""Kritik izin içeren rol atamasının onay kuralına bağlanması (PM kararı K21, PLT-09).

Kritik izinler `domain/roles.CRITICAL` (members.manage, approvals.rules.manage,
finance.charge.post, modules.manage). Kritik izin içeren rol atanırken `role_change` kuralı
varsa atama yapılmaz, talep açılır; onaylanınca yürütücü `assign_role(check_actor=False)` ile
atar (izin ve kendine atama denetimi talep açılırken yapılmıştır; rol/kapsam o arada
bozulduysa "onaylandı – yürütülemedi"). Ret ve geri çekmede atama hiç yapılmaz.
`services/roles.py` onay servisini içe aktaramaz (onay servisi rolleri kullanır).
"""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.roles import RoleRuleError, is_critical
from site_yonetim.models import Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import roles as roles_svc
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.roles import AssignmentInput


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def request_if_critical(
    session: AsyncSession,
    site: Site,
    *,
    user_id: uuid.UUID,
    user_name: str,
    data: AssignmentInput,
    requester: Person,
    actor_permissions: frozenset[Permission],
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    """Rol kritik değilse ya da `role_change` kuralı yoksa None (atama doğrudan yapılır).
    Denetimler (rol, kapsam, kendine atama, yetki aşımı) talep açılmadan yapılır."""
    role = await roles_svc.check_assignment(
        session,
        user_id=user_id,
        data=data,
        actor_id=requester.user_id,
        actor_permissions=actor_permissions,
        now=now,
    )
    if not is_critical(role.permissions):
        return None
    if (
        await approvals_svc.match_rule(
            session, ActionType.ROLE_CHANGE, amount=None, source=None, today=today
        )
        is None
    ):
        return None
    return await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.ROLE_CHANGE,
            title=f"Rol ataması — {user_name}, {role.name}",
            summary={"user": user_name, "role": role.name, "scope_kind": data.scope_kind},
            payload={
                "user_id": str(user_id),
                "role_key": role.key,
                "scope_kind": data.scope_kind,
                "block_id": str(data.block_id) if data.block_id else None,
                "starts_at": _iso(data.starts_at),
                "ends_at": _iso(data.ends_at),
            },
            subject_type="users",
            subject_id=user_id,
        ),
        requester=requester,
        today=today,
        now=now,
    )


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    scope = current_scope()
    site = await session.get(Site, scope.site_id) if scope else None
    if site is None:
        raise ExecutionError("Site bulunamadı.")
    now = request.decided_at or datetime.now().astimezone()
    data = AssignmentInput(
        role_key=payload["role_key"],
        scope_kind=payload["scope_kind"],
        block_id=uuid.UUID(payload["block_id"]) if payload.get("block_id") else None,
        starts_at=_parse(payload.get("starts_at")),
        ends_at=_parse(payload.get("ends_at")),
    )
    try:
        await roles_svc.assign_role(
            session,
            site,
            user_id=uuid.UUID(payload["user_id"]),
            data=data,
            actor_id=request.requested_by,
            actor_permissions=frozenset(),
            actor_name=request.requested_by_name,
            now=now,
            check_actor=False,
        )
    except RoleRuleError as exc:
        raise ExecutionError(f"Rol artık atanamıyor: {exc.message}") from exc
    except FormFieldError as exc:
        raise ExecutionError(f"Rol artık atanamıyor: {'; '.join(exc.fields.values())}") from exc


approvals_svc.register_executor(ActionType.ROLE_CHANGE, _execute)
