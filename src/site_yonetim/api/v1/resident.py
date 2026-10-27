"""Sakin uçları — docs/06 §2.14, docs/05 §6. Her şey üyelikteki `person_id` ile süzülür.

- Yalnız kişiye bağlı üyelik (sakin) kullanır; kişisi olmayan kullanıcı 403.
- Kendi bölümleri, kendi cari hesapları ve ekstresi, kendisine teslim edilen duyurular, kendi
  talepleri. Gider şeffaflığı K32 ile şeffaflık raporuna taşındı (`api/v1/transparency.py`):
  sakin artık giderleri ve fatura görüntüsünü tek tek görmez.
- Başka kişinin hesabı/talebi "yok"tur (404). Duyuru ve talep modülü kapalıysa ilgili uç 404.
- Talepler (HIZ-01, K18): kapanış onayı ve puan, iş başlamadan iptal, ücretli hizmet teklifine
  karar; tadilat/taşınma başvurusu (HIZ-05). İç not (K26) bu uçların hiçbirinde dönmez.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1 import announcements as announcements_api
from site_yonetim.api.v1 import requests as requests_api
from site_yonetim.api.v1 import service_desk as desk_api
from site_yonetim.api.v1.charges import LineOut
from site_yonetim.api.v1.payments import AccountOut, EntryOut, LastChargeOut, StatementOut
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.finance import LedgerSource
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.operations import (
    ACTIVE_STATUSES,
    STATUS_LABELS,
    OperationRuleError,
    RequestStatus,
)
from site_yonetim.domain.security import PackageStatus
from site_yonetim.domain.service_desk import PermitType, RuleError
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Block,
    LedgerAccount,
    LedgerEntry,
    Request,
    Unit,
    UnitParty,
)
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import announcements as announcements_svc
from site_yonetim.services import paid_services as paid_svc
from site_yonetim.services import permits as permits_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services import security as security_svc
from site_yonetim.services import service_desk as desk_svc

router = APIRouter(prefix="/sites/{slug}/resident", tags=["sakin"])
Paging = Annotated[PageParams, Depends()]
RECENT = 5
PERMITS_MAX = 100
OPEN = [s.value for s in ACTIVE_STATUSES]


async def resident(ctx: SiteContextDep) -> SiteContext:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx


Resident = Annotated[SiteContext, Depends(resident)]


def _person(ctx: SiteContext) -> uuid.UUID:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı zaten engeller
        raise ForbiddenError
    return person


def _module(ctx: SiteContext, key: ModuleKey) -> None:
    if key not in ctx.modules:
        raise NotFoundError


async def my_accounts(ctx: SiteContext) -> list[accounts_svc.AccountRow]:
    rows = await ctx.session.execute(
        accounts_svc.accounts_query()
        .where(LedgerAccount.person_id == _person(ctx))
        .order_by(LedgerAccount.reference_code)
    )
    return [accounts_svc.to_row(tuple(r)) for r in rows]


# --- Ana sayfa ----------------------------------------------------------------------


class MyUnit(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    role: PartyRole = Field(description="owner · tenant · resident · proxy")


class RecentEntry(BaseModel):
    account_id: uuid.UUID
    date: dt.date
    description: str
    debit: Money
    credit: Money


class MyRequest(BaseModel):
    id: uuid.UUID
    number: int
    title: str
    status: RequestStatus
    status_label: str


class PaymentInfo(BaseModel):
    """Sitenin kendi hesabı (platform parayı tutmaz, 6493). Havale açıklamasına hesabın
    `reference_code`'u yazılır (`accounts[]`)."""

    bank_name: str | None
    iban: str = Field(description="boşluksuz, büyük harf; biçimlendirmeyi frontend yapar")
    account_holder: str = Field(description="hesap sahibi: site adı")


class HomeOut(BaseModel):
    units: list[MyUnit] = Field(description="bugün malik/kiracı/oturan olduğu bölümler")
    accounts: list[AccountOut]
    total_balance: Money = Field(description="hesaplarının toplamı (negatif = avans)")
    recent_entries: list[RecentEntry] = Field(description="son 5 hareket")
    announcements: list[announcements_api.AnnouncementOut] | None = Field(
        description="son 3 duyuru (modül kapalıysa null)"
    )
    open_requests: list[MyRequest] | None = Field(
        description="açık talepleri (modül kapalıysa null)"
    )
    payment_info: PaymentInfo | None = Field(
        description='sitenin IBAN\'ı girilmemişse null ("ödeme bilgisi için yönetimle görüşün")'
    )


def _payment_info(ctx: SiteContext) -> PaymentInfo | None:
    site = ctx.site
    if not site.iban:
        return None
    return PaymentInfo(bank_name=site.bank_name, iban=site.iban, account_holder=site.name)


async def _units(ctx: SiteContext, today: dt.date) -> list[MyUnit]:
    rows = await ctx.session.execute(
        select(UnitParty.unit_id, UnitParty.role, Unit.number, Block.name)
        .join(Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(
            UnitParty.person_id == _person(ctx),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
        .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
    )
    return [
        MyUnit(
            unit_id=unit_id,
            unit_name=f"{block}-{number}" if block else number,
            role=PartyRole(role),
        )
        for unit_id, role, number, block in rows
    ]


@router.get("/home", summary="Sakin ana sayfası")
async def home(ctx: Resident, today: TodayDep) -> HomeOut:
    accounts = await my_accounts(ctx)
    account_ids = [a.account.id for a in accounts]
    entries = list(
        await ctx.session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.account_id.in_(account_ids))
            .order_by(LedgerEntry.date.desc(), LedgerEntry.created_at.desc())
            .limit(RECENT)
        )
    ) if account_ids else []  # fmt: skip
    announcements = None
    if ModuleKey.ANNOUNCEMENTS in ctx.modules:
        rows = list(
            await ctx.session.scalars(
                announcements_svc.visible_query(
                    show_all=False, person_id=_person(ctx), today=today
                ).limit(3)
            )
        )
        announcements = await announcements_api.out(ctx, rows)
    open_requests = None
    if ModuleKey.REQUESTS in ctx.modules:
        requests = await ctx.session.scalars(
            requests_svc.list_query(
                status=None, category=None, priority=None, reporter=_person(ctx)
            )
            .where(Request.status.in_(OPEN))
            .limit(RECENT)
        )
        open_requests = [
            MyRequest(
                id=r.id,
                number=r.number,
                title=r.title,
                status=RequestStatus(r.status),
                status_label=STATUS_LABELS[RequestStatus(r.status)],
            )
            for r in requests
        ]
    return HomeOut(
        units=await _units(ctx, today),
        accounts=[AccountOut.of(a, names=True) for a in accounts],
        total_balance=sum((a.balance for a in accounts), ZERO),
        recent_entries=[
            RecentEntry(
                account_id=e.account_id,
                date=e.date,
                description=e.description,
                debit=e.debit,
                credit=e.credit,
            )
            for e in entries
        ],
        announcements=announcements,
        open_requests=open_requests,
        payment_info=_payment_info(ctx),
    )


# --- Ekstre -------------------------------------------------------------------------


@router.get("/statement", summary="Kendi cari ekstresi")
async def statement(
    ctx: Resident,
    paging: Annotated[PageParams, Depends()],
    account_id: Annotated[
        uuid.UUID | None, Query(description="birden çok hesabı varsa; boşsa oturan hesabı")
    ] = None,
) -> StatementOut:
    accounts = await my_accounts(ctx)
    if account_id is not None:
        chosen = next((a for a in accounts if a.account.id == account_id), None)
    else:  # varsayılan: oturan hesabı (aidat), yoksa ilk hesap
        chosen = next(
            (a for a in accounts if a.account.kind == AccountKind.OCCUPANT.value), None
        ) or (accounts[0] if accounts else None)
    if chosen is None:
        raise NotFoundError("Cari hesap bulunamadı.")
    lines, total = await accounts_svc.statement(
        ctx.session, chosen.account.id, offset=paging.offset, limit=paging.page_size
    )
    last = await accounts_svc.last_charge(ctx.session, chosen.account.id)
    return StatementOut(
        account=AccountOut.of(chosen, names=True),
        entries=Page(
            items=[
                EntryOut(
                    id=line.entry.id,
                    date=line.entry.date,
                    due_date=line.entry.due_date,
                    debit=line.entry.debit,
                    credit=line.entry.credit,
                    source=LedgerSource(line.entry.source),
                    source_id=line.entry.source_id,
                    description=line.entry.description,
                    running_balance=line.running_balance,
                )
                for line in lines
            ],
            page=paging.page,
            page_size=paging.page_size,
            total=total,
        ),
        last_charge=LastChargeOut(
            period=last.period.name,
            amount=last.charge.amount,
            lines=[LineOut.of(line) for line in last.lines],
        )
        if last
        else None,
    )


# --- Duyurular ----------------------------------------------------------------------


@router.get("/announcements", summary="Kendisine hedeflenen duyurular")
async def my_announcements(
    ctx: Resident, paging: Paging, today: TodayDep
) -> Page[announcements_api.AnnouncementOut]:
    _module(ctx, ModuleKey.ANNOUNCEMENTS)
    query = announcements_svc.visible_query(show_all=False, person_id=_person(ctx), today=today)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await announcements_api.out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


# --- Talepler -----------------------------------------------------------------------


@router.get("/requests", summary="Kendi talepleri")
async def my_requests(ctx: Resident, paging: Paging, now: NowDep) -> Page[requests_api.RequestOut]:
    _module(ctx, ModuleKey.REQUESTS)
    query = requests_svc.list_query(
        status=None, category=None, priority=None, reporter=_person(ctx)
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await requests_api.out(ctx, rows, now),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post("/requests", status_code=status.HTTP_201_CREATED, summary="Yeni talep")
async def new_request(
    ctx: Resident,
    body: requests_api.RequestCreate,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[requests_api.RequestDetail]:
    """Talep her zaman sakinin kendi adına; bölüm verilirse kendi bölümü olmalı (422). Öncelik
    kategoriden gelir."""
    _module(ctx, ModuleKey.REQUESTS)
    return await requests_api.create_for(
        ctx, body.model_copy(update={"reported_by_person_id": None}), current, today, now, own=True
    )


async def _my_request(ctx: SiteContext, request_id: uuid.UUID) -> Request:
    _module(ctx, ModuleKey.REQUESTS)
    request = await requests_svc.get(ctx.session, request_id, for_update=True)
    if request is None or request.reported_by_person_id != _person(ctx):
        raise NotFoundError("Talep bulunamadı.")
    return request


class RequestConfirmIn(BaseModel):
    solved: bool = Field(description="true: sorun çözüldü (puan zorunlu); false: devam ediyor")
    score: int | None = Field(default=None, description="1–5")
    comment: str | None = Field(default=None, max_length=300)


@router.post("/requests/{request_id}/confirm", summary="Kapanış onayı ve memnuniyet puanı")
async def confirm_request(
    request_id: uuid.UUID,
    body: RequestConfirmIn,
    ctx: Resident,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[requests_api.RequestDetail]:
    """Yalnız "çözüldü" talep (409 `not_resolved`). Çözüldü → talep kapanır; devam ediyor →
    yeniden açılır. Puan ≤ eşik yöneticiye bildirilir."""
    request = await _my_request(ctx, request_id)
    try:
        await desk_svc.confirm(
            ctx.session, ctx.site, request, solved=body.solved, score=body.score,
            comment=body.comment, person_id=_person(ctx), actor=current.user.full_name, now=now,
        )  # fmt: skip
    except RuleError as exc:
        raise requests_api.form_error(exc) from exc
    except OperationRuleError as exc:
        raise requests_api.rule_error(exc) from exc
    await ctx.session.commit()
    message = (
        "Teşekkürler; talep kapatıldı."
        if body.solved
        else "Talebiniz yeniden açıldı ve yönetime iletildi."
    )
    return Written(data=await requests_api.detail(ctx, request, now), message=message)


class RequestCancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/requests/{request_id}/cancel", summary="Talebi iptal et (iş başlamadan)")
async def cancel_request(
    request_id: uuid.UUID,
    body: RequestCancelIn,
    ctx: Resident,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[requests_api.RequestDetail]:
    request = await _my_request(ctx, request_id)
    try:
        await requests_svc.cancel_by_resident(
            ctx.session, request, body.reason, actor=current.user.full_name, now=now
        )
    except OperationRuleError as exc:
        raise requests_api.rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await requests_api.detail(ctx, request, now),
        message=f"#{request.number} numaralı talebiniz iptal edildi.",
    )


class QuoteDecisionIn(BaseModel):
    decision: Literal["accept", "reject"]


@router.post("/quotes/{quote_id}/decision", summary="Ücretli hizmet teklifine karar")
async def quote_decision(
    quote_id: uuid.UUID,
    body: QuoteDecisionIn,
    ctx: Resident,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[requests_api.ServiceQuoteOut]:
    """Karar, teklifin bölümünde bugün malik/kiracı/oturan olan sakindedir. Teklif aşamasında
    onay/ret; gerçekleşen teklifi aşmışsa ek tutar onayı/reddi (ret → teklif tutarı)."""
    _module(ctx, ModuleKey.REQUESTS)
    quote = await paid_svc.get_quote(ctx.session, quote_id)
    units = await requests_svc.person_unit_ids(ctx.session, _person(ctx), today)
    request = await requests_svc.get(ctx.session, quote.request_id) if quote else None
    if quote is None or request is None or quote.unit_id not in units:
        raise NotFoundError("Teklif bulunamadı.")
    try:
        await paid_svc.decide(
            ctx.session, quote, request, accept=body.decision == "accept",
            actor=current.user.full_name, now=now, today=today,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise requests_api.rule_error(exc) from exc
    lines = (await paid_svc.lines_of(ctx.session, [quote.id]))[quote.id]
    data = requests_api.ServiceQuoteOut.of(quote, lines)
    await ctx.session.commit()
    if quote.billed_total is not None:
        message = f"Kararınız kaydedildi; {format_money_tr(quote.billed_total)} borcunuza yansıdı."
    elif body.decision == "accept":
        message = "Teklifi onayladınız; iş planlanacak."
    else:
        message = "Teklifi reddettiniz; iş ücretli yapılmayacak."
    return Written(data=data, message=message)


# --- Tadilat ve taşınma --------------------------------------------------------------------


class MyPermitsOut(BaseModel):
    rules: desk_api.PermitRulesOut
    items: list[desk_api.PermitOut]


@router.get("/permits", summary="Tadilat ve taşınma başvurularım + kurallar")
async def my_permits(ctx: Resident, today: TodayDep) -> MyPermitsOut:
    _module(ctx, ModuleKey.REQUESTS)
    units = await requests_svc.person_unit_ids(ctx.session, _person(ctx), today)
    rows = list(
        await ctx.session.scalars(
            permits_svc.list_query(status=None, unit_ids=units).limit(PERMITS_MAX)
        )
    )
    return MyPermitsOut(
        rules=desk_api.PermitRulesOut.of(await permits_svc.rules(ctx.session)),
        items=await desk_api.permits_out(ctx, rows),
    )


class PermitApplyIn(BaseModel):
    unit_id: uuid.UUID | None = Field(default=None, description="boş: ilk bölümü")
    type: PermitType
    date_from: dt.date
    date_to: dt.date | None = Field(default=None, description="yalnız tadilatta")
    contractor: str | None = Field(default=None, max_length=80)
    description: str = Field(max_length=500)


@router.post("/permits", status_code=status.HTTP_201_CREATED, summary="Tadilat/taşınma başvurusu")
async def apply_permit(
    body: PermitApplyIn, ctx: Resident, current: CurrentUserDep, today: TodayDep
) -> Written[desk_api.PermitOut]:
    _module(ctx, ModuleKey.REQUESTS)
    units = await _units(ctx, today)
    own = {u.unit_id for u in units if u.role is not PartyRole.PROXY}
    unit_id = body.unit_id or next(iter(u.unit_id for u in units if u.unit_id in own), None)
    if unit_id is None or unit_id not in own:
        message = "Yalnız kendi bölümünüz için başvurabilirsiniz."
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "unit_not_yours", message,
                       {"unit_id": message})  # fmt: skip
    try:
        permit = await permits_svc.apply(
            ctx.session,
            permits_svc.PermitData(
                unit_id, body.type, body.date_from, body.date_to, body.contractor,
                body.description,
            ),
            person_id=_person(ctx), actor=current.user.full_name, today=today,
        )  # fmt: skip
    except RuleError as exc:
        raise requests_api.form_error(exc) from exc
    [data] = await desk_api.permits_out(ctx, [permit])
    await ctx.session.commit()
    message = (
        "Tadilat başvurunuz yönetime iletildi; onaylanınca çalışma saatleri size bildirilir."
        if body.type is PermitType.RENOVATION
        else "Taşınma bildiriminiz alındı; asansör ve yükleme alanı için rezervasyon önerilir."
    )
    return Written(data=data, message=message)


# --- Kargolarım -------------------------------------------------------------------------


class MyPackage(BaseModel):
    id: uuid.UUID
    unit_name: str
    carrier: str | None
    received_at: dt.datetime
    pickup_code: str = Field(description="teslim kodu — kargoyu alırken güvenliğe söylenir")


@router.get("/packages", summary="Bekleyen kargolarım (teslim koduyla)")
async def my_packages(ctx: Resident, today: TodayDep) -> list[MyPackage]:
    """Kişinin bugün bağlı olduğu bölümlere gelen, teslim bekleyen kargolar. Teslim kodu yalnız
    burada görünür (güvenlik uçları kodu döndürmez)."""
    _module(ctx, ModuleKey.PACKAGES)
    units = await security_svc.person_unit_ids(ctx.session, _person(ctx), today)
    rows = list(
        await ctx.session.scalars(
            security_svc.packages_query(status=PackageStatus.WAITING, unit_ids=units).limit(50)
        )
    )
    names = await security_svc.unit_names(ctx.session, {p.unit_id for p in rows})
    return [
        MyPackage(
            id=p.id,
            unit_name=names.get(p.unit_id, "—"),
            carrier=p.carrier,
            received_at=p.received_at,
            pickup_code=p.pickup_code,
        )
        for p in rows
    ]
