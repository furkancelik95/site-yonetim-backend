"""Borç takibi — Yazılım Dokümanı FIN-12, servis isteği 29: yaşlandırma, takip özeti, zaman
çizelgesi, hatırlatma/arama/not kaydı, ihtarname gönderimi ve tebliğ.

Okuma `finance.read`; kayıt `finance.payment.record`. **Gönderim K5'e bağlı** (SMS/bildirim
sağlayıcısı yok): hatırlatma yalnız kayda geçer, yanıt mesajı bunu söyler. Borçlu bilgisi hiçbir
sakin ucunda dönmez. "İhtarname" onay kuralı varsa ihtarname kaydı 202 (onay bekler).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import FinanceRead
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.debt_tracking import ActionKind, CollectionChannel
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_date_tr
from site_yonetim.models import CollectionAction, DemandLetter, LedgerAccount
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import debt_tracking as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}", tags=["borç takibi"])
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]
Paging = Annotated[PageParams, Depends()]
BULK_MAX = 500


async def _account(ctx: SiteContext, account_id: uuid.UUID) -> LedgerAccount:
    row = await accounts_svc.account_row(ctx.session, account_id)
    if row is None:
        raise NotFoundError("Hesap bulunamadı.")
    return row.account


# --- Yaşlandırma ve takip özeti --------------------------------------------------------


class AgingBucketOut(BaseModel):
    key: str = Field(description="0_30 · 31_60 · 61_90 · 90_plus")
    label: str
    count: int
    amount: Money


class AgingBlockOut(BaseModel):
    block: str
    d0_30: Money = Field(alias="0_30", serialization_alias="0_30")
    d31_60: Money = Field(alias="31_60", serialization_alias="31_60")
    d61_90: Money = Field(alias="61_90", serialization_alias="61_90")
    d90_plus: Money = Field(alias="90_plus", serialization_alias="90_plus")
    total: Money


class AgingOut(BaseModel):
    as_of: dt.date
    total: Money = Field(description="borçlular listesinin toplam bakiyesine eşit")
    buckets: list[AgingBucketOut]
    by_block: list[AgingBlockOut]


@router.get("/receivables/aging", summary="Alacak yaşlandırması (0–30 / 31–60 / 61–90 / 90+)")
async def aging(ctx: FinanceRead, today: TodayDep) -> AgingOut:
    result = await svc.aging(ctx.session, today)
    return AgingOut(
        as_of=result.as_of,
        total=sum((b[3] for b in result.buckets), ZERO),
        buckets=[
            AgingBucketOut(key=b.value, label=label, count=count, amount=amount)
            for b, label, count, amount in result.buckets
        ],
        by_block=[
            AgingBlockOut.model_validate(
                {
                    "block": name,
                    **{b.value: f"{v:.2f}" for b, v in values.items()},
                    "total": f"{total:.2f}",
                }
            )
            for name, values, total in result.by_block
        ],
    )


class CollectionFollowUpOut(BaseModel):
    plan_status: str | None = Field(description="active · broken · completed · cancelled …")
    letter_status: str | None = Field(description="approval_pending · sent · served")
    last_action_at: dt.datetime | None
    reminder_count: int


@router.get("/collection-status", summary="Hesap → takip özeti (kaydı olan hesaplar)")
async def collection_status(ctx: FinanceRead, today: TodayDep) -> dict[str, CollectionFollowUpOut]:
    return {
        str(account_id): CollectionFollowUpOut(
            plan_status=f.plan_status,
            letter_status=f.letter_status,
            last_action_at=f.last_action_at,
            reminder_count=f.reminder_count,
        )
        for account_id, f in (await svc.status_map(ctx.session, today)).items()
    }


# --- Zaman çizelgesi --------------------------------------------------------------------


class DemandLetterOut(BaseModel):
    id: uuid.UUID
    method: Literal["hand", "registered_mail", "notary"]
    sent_on: dt.date
    tracking_no: str | None
    served_on: dt.date | None
    status: Literal["approval_pending", "sent", "served", "rejected"]
    approval_request_id: uuid.UUID | None


def _letter(letter: DemandLetter) -> DemandLetterOut:
    return DemandLetterOut(
        id=letter.id,
        method=letter.method,  # type: ignore[arg-type]
        sent_on=letter.sent_on,
        tracking_no=letter.tracking_no,
        served_on=letter.served_on,
        status=letter.status,  # type: ignore[arg-type]
        approval_request_id=letter.approval_request_id,
    )


class TimelinePlanOut(BaseModel):
    id: uuid.UUID
    status: str


class CollectionTimelineItemOut(BaseModel):
    id: uuid.UUID
    kind: Literal["reminder", "call", "note", "demand_letter", "payment_plan"]
    channel: str | None = Field(description="app · sms · phone; ihtarnamede gönderim yöntemi")
    note: str | None
    stage: str | None = Field(description="gece işinin kademesi (app · sms · call · demand_letter)")
    by: str
    at: dt.datetime
    letter: DemandLetterOut | None = None
    plan: TimelinePlanOut | None = None


@router.get("/accounts/{account_id}/collection-timeline", summary="Hesabın takip geçmişi")
async def timeline(
    account_id: uuid.UUID, ctx: FinanceRead, paging: Paging, today: TodayDep
) -> Page[CollectionTimelineItemOut]:
    await _account(ctx, account_id)
    items = await svc.timeline(ctx.session, account_id, today)
    page = items[paging.offset : paging.offset + paging.page_size]
    return Page(
        items=[
            CollectionTimelineItemOut(
                id=i.id,
                kind=i.kind,  # type: ignore[arg-type]
                channel=i.channel,
                note=i.note,
                stage=i.stage,
                by=i.by,
                at=i.at,
                letter=_letter(i.letter) if i.letter else None,
                plan=TimelinePlanOut(id=i.plan_id, status=i.plan_status or "")
                if i.plan_id
                else None,
            )
            for i in page
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=len(items),
    )


# --- Kayıt ------------------------------------------------------------------------------


class CollectionActionIn(BaseModel):
    kind: str | None = Field(default=None, description="reminder · call · note")
    channel: str | None = Field(default=None, description="hatırlatmada app · sms")
    note: str | None = Field(default=None, max_length=500)


class CollectionActionOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    kind: ActionKind
    channel: CollectionChannel | None
    note: str | None
    by: str
    at: dt.datetime
    delivered: bool = Field(description="mesaj gönderildi mi — K5 gelene kadar her zaman false")


def _action(a: CollectionAction) -> CollectionActionOut:
    return CollectionActionOut(
        id=a.id,
        account_id=a.account_id,
        kind=ActionKind(a.kind),
        channel=CollectionChannel(a.channel) if a.channel else None,
        note=a.note,
        by=a.by_name,
        at=a.created_at,
        delivered=False,
    )


@router.post(
    "/accounts/{account_id}/collection-actions",
    status_code=HTTPStatus.CREATED,
    summary="Hatırlatma / arama / not kaydı",
)
async def add_action(
    account_id: uuid.UUID, body: CollectionActionIn, ctx: PaymentRecord, current: CurrentUserDep
) -> Written[CollectionActionOut]:
    account = await _account(ctx, account_id)
    try:
        action = await svc.add_action(
            ctx.session,
            account,
            kind=body.kind,
            channel=body.channel,
            note=body.note,
            actor=actor(current),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    message = svc.action_message(
        ActionKind(action.kind), CollectionChannel(action.channel) if action.channel else None
    )
    return Written(data=_action(action), message=message)


class BulkReminderIn(BaseModel):
    account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=BULK_MAX)
    channel: Literal["app", "sms"] = "app"


class BulkReminderOut(BaseModel):
    count: int
    delivered: bool = False


@router.post(
    "/collection-actions/bulk", status_code=HTTPStatus.CREATED, summary="Toplu hatırlatma kaydı"
)
async def bulk(
    body: BulkReminderIn, ctx: PaymentRecord, current: CurrentUserDep
) -> Written[BulkReminderOut]:
    if not body.account_ids:
        raise form_error(FormFieldError({"account_ids": "En az bir hesap seçin."}))
    count = await svc.bulk_reminders(
        ctx.session, body.account_ids, channel=body.channel, actor=actor(current)
    )
    await ctx.session.commit()
    return Written(
        data=BulkReminderOut(count=count),
        message=f"{count} hesap için hatırlatma kaydedildi. {svc.NOT_SENT}",
    )


# --- İhtarname --------------------------------------------------------------------------


class DemandLetterIn(BaseModel):
    method: str | None = Field(default=None, description="hand · registered_mail · notary")
    sent_on: dt.date | None = None
    tracking_no: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/accounts/{account_id}/demand-letters",
    status_code=HTTPStatus.CREATED,
    summary="İhtarname gönderimini kaydet (onay kuralı varsa 202)",
    responses={202: {"description": "Onaya gönderildi"}},
)
async def add_letter(
    account_id: uuid.UUID,
    body: DemandLetterIn,
    ctx: PaymentRecord,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[DemandLetterOut]:
    account = await _account(ctx, account_id)
    try:
        result = await svc.add_letter(
            ctx.session,
            ctx.site,
            account,
            method=body.method,
            sent_on=body.sent_on,
            tracking_no=body.tracking_no,
            note=body.note,
            actor=actor(current),
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    await ctx.session.commit()
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=_letter(result.letter),
            message=f"İhtarname onaya gönderildi (talep #{result.request.number}); onaylanınca "
            "gönderim kaydı kesinleşir.",
        )
    return Written(
        data=_letter(result.letter),
        message="İhtarname gönderimi kaydedildi. Tebliğ edilince tebliğ tarihini girin.",
    )


class LetterServedIn(BaseModel):
    served_on: dt.date | None = None


@router.patch("/demand-letters/{letter_id}", summary="Tebliğ tarihini kaydet")
async def mark_served(
    letter_id: uuid.UUID, body: LetterServedIn, ctx: PaymentRecord, today: TodayDep
) -> Written[DemandLetterOut]:
    letter = await svc.get_letter(ctx.session, letter_id)
    if letter is None:
        raise NotFoundError("İhtarname bulunamadı.")
    if body.served_on is None:
        raise form_error(FormFieldError({"served_on": "Tebliğ tarihini girin."}))
    try:
        await svc.mark_served(ctx.session, letter, body.served_on, today)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.LetterStateError as exc:
        raise ApiError(HTTPStatus.CONFLICT, "invalid_status", str(exc)) from exc
    await ctx.session.commit()
    return Written(
        data=_letter(letter),
        message=f"Tebliğ {format_date_tr(body.served_on)} olarak kaydedildi.",
    )
