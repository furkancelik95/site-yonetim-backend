"""Tesis kurulum durumu ve canlıya geçiş — servis isteği 26 (YAP-01).

`modules.manage` (site yöneticisi). Kontrol listesi her çağrıda gerçek veriden hesaplanır;
canlıya alma zorunlu maddeler tamamsa yapılır, geri alınmaz ve denetim kaydına yazılır.
"""

import datetime as dt
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from site_yonetim.api.deps import NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Written
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.access import Permission
from site_yonetim.models.audit import AuditAction, current_actor, record
from site_yonetim.services import setup as svc
from site_yonetim.services.vehicles import settings

router = APIRouter(prefix="/sites/{slug}/setup", tags=["kurulum"])
ModulesManage = Annotated[SiteContext, Depends(require_permission(Permission.MODULES_MANAGE))]


class SetupChecklistItemOut(BaseModel):
    key: str
    label: str
    ok: bool
    detail: str
    link: str | None = Field(description="ilgili ekran: `/s/{slug}/{link}`")
    required: bool


class SetupOut(BaseModel):
    status: Literal["setup", "live"]
    went_live_at: dt.datetime | None
    finance_mode: Literal["platform"] = Field(
        description="Mod K / KiviERP ortak kararı (A-01) bekliyor"
    )
    ready: bool = Field(description="zorunlu maddelerin hepsi tamam")
    opening_reconciled_at: dt.datetime | None
    checklist: list[SetupChecklistItemOut]


async def _out(ctx: SiteContext, today: dt.date) -> SetupOut:
    items = await svc.checklist(ctx.session, ctx.site, today)
    config = await settings(ctx.session)
    return SetupOut(
        status=ctx.site.lifecycle_status,  # type: ignore[arg-type]
        went_live_at=ctx.site.went_live_at,
        finance_mode=svc.FINANCE_MODE,
        ready=svc.is_ready(items),
        opening_reconciled_at=config.opening_reconciled_at,
        checklist=[
            SetupChecklistItemOut(
                key=i.key, label=i.label, ok=i.ok, detail=i.detail, link=i.link,
                required=i.required,
            )
            for i in items
        ],
    )  # fmt: skip


@router.get("", summary="Kurulum durumu ve canlıya geçiş kontrol listesi")
async def get_setup(ctx: ModulesManage, today: TodayDep) -> SetupOut:
    out = await _out(ctx, today)
    await ctx.session.commit()  # ayar satırı yoksa açılır
    return out


class GoLiveOut(BaseModel):
    status: Literal["live"]
    went_live_at: dt.datetime


@router.post("/go-live", summary="Canlıya al (geri alınmaz)")
async def go_live(ctx: ModulesManage, today: TodayDep, now: NowDep) -> Written[GoLiveOut]:
    site = await ctx.session.merge(ctx.site)
    try:
        await svc.go_live(ctx.session, site, today=today, now=now)
    except svc.SetupRuleError as exc:
        raise ApiError(HTTPStatus.CONFLICT, exc.code, exc.message) from None
    record(
        ctx.session, AuditAction.UPDATE, "sites", site.id,
        {"lifecycle_status": "live", "went_live_at": now.isoformat()},
        reason="Tesis canlıya alındı",
    )  # fmt: skip
    await ctx.session.commit()
    return Written(
        data=GoLiveOut(status="live", went_live_at=now),
        message="Tesis canlıya alındı; artık davet gönderilebilir.",
    )


class OpeningReconciledIn(BaseModel):
    note: str = Field(
        min_length=3,
        max_length=300,
        description="mutabakat açıklaması (ör. imzalı mutabakat belgesi tarihi/numarası)",
    )


@router.post("/opening-reconciled", summary="Açılış bakiyesi mutabakatı yapıldı işareti")
async def opening_reconciled(
    ctx: ModulesManage, body: OpeningReconciledIn, today: TodayDep, now: NowDep
) -> Written[SetupOut]:
    """Aktarım yerine elle girilmiş/denetlenmiş açılış için: yöneticinin beyanı, denetimli."""
    config = await settings(ctx.session)
    actor = current_actor()
    config.opening_reconciled_at = now
    config.opening_reconciled_by = actor.name if actor else None
    config.opening_reconciled_note = " ".join(body.note.split())
    await ctx.session.flush()
    out = await _out(ctx, today)
    await ctx.session.commit()
    return Written(data=out, message="Açılış bakiyesi mutabakatı işaretlendi.")
