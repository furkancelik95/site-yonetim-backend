"""Nöbet defteri ve vardiya devri — servis isteği 45 (Yazılım Dokümanı GUV-06).

Modül `visitors` (kapalıysa 404); izin `security.incidents` (Güvenlik yazar, Yönetici okur ve
yazar). Kayıt değişmez ve silinmez; düzeltme ek notla. Devri yazan kendi devrini onaylayamaz.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.gatehouse import access, rule_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.gatehouse import ShiftLogKind
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import ShiftLogEntry
from site_yonetim.services import gatehouse as gate_svc
from site_yonetim.services import shift_log as svc

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])
Log = Annotated[
    SiteContext, Depends(access((ModuleKey.VISITORS,), (Permission.SECURITY_INCIDENTS,)))
]


class ShiftLogAddendumOut(BaseModel):
    text: str
    by: str
    at: dt.datetime


class ShiftLogEntryOut(BaseModel):
    id: uuid.UUID
    kind: ShiftLogKind
    text: str
    gate_id: uuid.UUID | None
    by: str
    by_me: bool = Field(description="kayıt bu kullanıcının mı (kendi devrini onaylayamaz)")
    at: dt.datetime
    addenda: list[ShiftLogAddendumOut]
    acknowledged_by: str | None
    acknowledged_at: dt.datetime | None


def _out(view: svc.EntryView, me: uuid.UUID) -> ShiftLogEntryOut:
    e = view.entry
    return ShiftLogEntryOut(
        id=e.id,
        kind=ShiftLogKind(e.kind),
        text=e.text,
        gate_id=e.gate_id,
        by=e.author_name,
        by_me=e.author_user_id == me,
        at=e.at,
        addenda=[ShiftLogAddendumOut(text=a.text, by=a.author_name, at=a.at) for a in view.addenda],
        acknowledged_by=view.ack.user_name if view.ack else None,
        acknowledged_at=view.ack.at if view.ack else None,
    )


def _author(current: CurrentUserDep) -> svc.Author:
    return svc.Author(current.user.id, current.user.full_name)


@router.get("/shift-log", summary="Nöbet defteri (gün, tesis saatiyle; yeni üstte)")
async def list_log(
    ctx: Log,
    current: CurrentUserDep,
    today: TodayDep,
    day: Annotated[dt.date | None, Query(alias="date", description="boşsa bugün")] = None,
) -> list[ShiftLogEntryOut]:
    return [_out(v, current.user.id) for v in await svc.day(ctx.session, day or today)]


class ShiftLogEntryIn(BaseModel):
    kind: str | None = Field(default=None, description="note · event · handover")
    text: str | None = Field(default=None, max_length=5000)
    gate_id: uuid.UUID | None = None


@router.post("/shift-log", status_code=status.HTTP_201_CREATED, summary="Deftere yaz")
async def write(
    ctx: Log, body: ShiftLogEntryIn, current: CurrentUserDep, now: NowDep
) -> Written[ShiftLogEntryOut]:
    try:
        await gate_svc.require_gate(ctx.session, body.gate_id)
        entry = await svc.write(
            ctx.session,
            kind=body.kind,
            text=body.text,
            gate_id=body.gate_id,
            author=_author(current),
            now=now,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [view] = await svc.views(ctx.session, [entry])
    message = (
        "Devir notu yazıldı; sonraki görevli okuyup onaylayacak."
        if entry.kind == ShiftLogKind.HANDOVER.value
        else "Nöbet defterine yazıldı. Kayıt değiştirilemez; düzeltme ek notla yapılır."
    )
    return Written(data=_out(view, current.user.id), message=message)


async def _entry(ctx: SiteContext, entry_id: uuid.UUID) -> ShiftLogEntry:
    entry = await svc.get(ctx.session, entry_id)
    if entry is None:
        raise NotFoundError("Kayıt bulunamadı.")
    return entry


class ShiftLogAddendumIn(BaseModel):
    text: str | None = Field(default=None, max_length=2000)


@router.post("/shift-log/{entry_id}/addenda", summary="Ek not")
async def add_addendum(
    entry_id: uuid.UUID, ctx: Log, body: ShiftLogAddendumIn, current: CurrentUserDep, now: NowDep
) -> Written[ShiftLogEntryOut]:
    entry = await _entry(ctx, entry_id)
    try:
        await svc.add_addendum(ctx.session, entry, text=body.text, author=_author(current), now=now)
    except FormFieldError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [view] = await svc.views(ctx.session, [entry])
    return Written(data=_out(view, current.user.id), message="Ek not eklendi.")


@router.post("/shift-log/{entry_id}/acknowledge", summary="Devri onayla (Okudum, devraldım)")
async def acknowledge(
    entry_id: uuid.UUID, ctx: Log, current: CurrentUserDep, now: NowDep
) -> Written[ShiftLogEntryOut]:
    entry = await svc.get(ctx.session, entry_id)
    if entry is None or entry.kind != ShiftLogKind.HANDOVER.value:
        raise NotFoundError("Devir notu bulunamadı.")
    try:
        await svc.acknowledge(ctx.session, entry, author=_author(current), now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [view] = await svc.views(ctx.session, [entry])
    return Written(data=_out(view, current.user.id), message="Devir okundu olarak onaylandı.")
