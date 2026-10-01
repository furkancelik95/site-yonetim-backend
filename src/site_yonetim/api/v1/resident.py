"""Sakin uçları — docs/06 §2.14, docs/05 §6. Her şey üyelikteki `person_id` ile süzülür.

- Yalnız kişiye bağlı üyelik (sakin) kullanır; kişisi olmayan kullanıcı 403.
- Kendi bölümleri, kendi cari hesapları ve ekstresi, kendisine teslim edilen duyurular, kendi
  talepleri; sitenin gerçekleşen giderleri ve fatura görüntüsü (şeffaflık).
- Başka kişinin hesabı/talebi "yok"tur (404). Duyuru ve talep modülü kapalıysa ilgili uç 404.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1 import announcements as announcements_api
from site_yonetim.api.v1 import requests as requests_api
from site_yonetim.api.v1.charges import LineOut
from site_yonetim.api.v1.expenses import StoreDep, send_document
from site_yonetim.api.v1.payments import AccountOut, EntryOut, LastChargeOut, StatementOut
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.finance import LedgerSource
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.operations import STATUS_LABELS, RequestStatus
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.models import (
    Block,
    Expense,
    ExpenseCategory,
    LedgerAccount,
    LedgerEntry,
    Request,
    Unit,
    UnitParty,
)
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import announcements as announcements_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.expenses import realized, stored_file

router = APIRouter(prefix="/sites/{slug}/resident", tags=["sakin"])
Paging = Annotated[PageParams, Depends()]
RECENT = 5
OPEN = [RequestStatus.OPEN.value, RequestStatus.IN_PROGRESS.value, RequestStatus.WAITING.value]


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


async def _my_accounts(ctx: SiteContext) -> list[accounts_svc.AccountRow]:
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
    accounts = await _my_accounts(ctx)
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
    accounts = await _my_accounts(ctx)
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
async def my_requests(ctx: Resident, paging: Paging) -> Page[requests_api.RequestOut]:
    _module(ctx, ModuleKey.REQUESTS)
    query = requests_svc.list_query(
        status=None, category=None, priority=None, reporter=_person(ctx)
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await requests_api.out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post("/requests", status_code=status.HTTP_201_CREATED, summary="Yeni talep")
async def new_request(
    ctx: Resident, body: requests_api.RequestCreate, current: CurrentUserDep, today: TodayDep
) -> Written[requests_api.RequestDetail]:
    """Talep her zaman sakinin kendi adına; bölüm verilirse kendi bölümü olmalı (422)."""
    _module(ctx, ModuleKey.REQUESTS)
    return await requests_api.create_for(
        ctx, body.model_copy(update={"reported_by_person_id": None}), current, today, own=True
    )


# --- Site giderleri (şeffaflık) --------------------------------------------------------


class SiteExpense(BaseModel):
    id: uuid.UUID
    date: dt.date
    description: str
    category: str
    vendor: str | None
    amount: Money
    is_paid: bool
    document_id: uuid.UUID | None = Field(description="fatura görüntüsü: GET …/resident/files/{id}")


class SiteExpensesOut(Page[SiteExpense]):
    total_amount: Money = Field(description="seçilen yılın gerçekleşen gideri")


@router.get("/expenses", summary="Sitenin gider dökümü (gerçekleşen)")
async def site_expenses(
    ctx: Resident,
    paging: Paging,
    today: TodayDep,
    year: Annotated[int | None, Query(ge=2000, le=2100, description="boşsa bu yıl")] = None,
) -> SiteExpensesOut:
    chosen = year or today.year
    conditions = [realized(), func.extract("year", Expense.date) == chosen]
    total = (
        await ctx.session.scalar(select(func.count()).select_from(Expense).where(*conditions)) or 0
    )
    amount = await ctx.session.scalar(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(*conditions)
    )
    rows = await ctx.session.execute(
        select(Expense, ExpenseCategory.name)
        .join(
            ExpenseCategory,
            and_(
                ExpenseCategory.id == Expense.expense_category_id,
                ExpenseCategory.site_id == Expense.site_id,
            ),
        )
        .where(*conditions)
        .order_by(Expense.date.desc(), Expense.created_at.desc())
        .offset(paging.offset)
        .limit(paging.page_size)
    )
    return SiteExpensesOut(
        items=[
            SiteExpense(
                id=e.id,
                date=e.date,
                description=e.description,
                category=category,
                vendor=e.vendor,
                amount=e.amount,
                is_paid=e.paid_on is not None,
                document_id=e.stored_file_id,
            )
            for e, category in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
        total_amount=amount or ZERO,
    )


@router.get(
    "/files/{file_id}",
    summary="Gider belgesi (fatura görüntüsü)",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/*": {}}}},
)
async def expense_document(file_id: uuid.UUID, ctx: Resident, store: StoreDep) -> Response:
    """Yalnız gerçekleşen bir giderin belgesi; başka dosya "yok"tur."""
    linked = await ctx.session.scalar(
        select(Expense.id).where(Expense.stored_file_id == file_id, realized()).limit(1)
    )
    row = await stored_file(ctx.session, file_id) if linked else None
    if row is None:
        raise NotFoundError("Belge bulunamadı.")
    return send_document(row, store)
