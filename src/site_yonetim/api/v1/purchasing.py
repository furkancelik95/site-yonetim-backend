"""Satın alma uçları — Yazılım Dokümanı SAT-01..06, servis istekleri 62–67.

Modül kapısı yok (finans çekirdeğinin parçası; doküman modül listesi genişleyince `purchasing`
anahtarı eklenecek — docs/12). Yetki:
- Okuma (tedarikçi, talep, sipariş, fatura, ödenecekler, bütçe, ayar) `expenses.read`.
- Tedarikçi kartı, ayarlar, sipariş, fatura, fark kararı ve "ödendi" `expenses.manage`.
- Talep açma, teklif, tedarikçi linki, seçim, iptal: `expenses.read` + (`requests.assign` ya da
  `expenses.manage`).
- Mal/hizmet kabul: `expenses.read` + (`requests.assign`, `inventory.manage` ya da
  `expenses.manage`) — stok girişi sistem adına yazılır, stok yetkisi aranmaz.
- Onay kararı: onay kuralı varsa onay mekanizmasının atadığı onaycı; yoksa `expenses.manage`.
  Talep eden kendi talebini onaylayamaz (403 `self_approval`).

İki uç **herkese açık** (`/public/quote-links/{token}`, oturum yok — `tests/architecture/
test_routes.py` beyaz listesinde) ve bir belge ekleme ucu: IP başına istek sınırı, belirteç 256
bit, yalnız özeti saklanır; bilinmeyen belirteç 404, kapanmış link 410.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.expenses import StoreDep
from site_yonetim.api.v1.finance_common import (
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ApprovalStatus
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.management import format_quantity
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.purchasing import (
    DecisionKind,
    DecisionStage,
    InvoiceStatus,
    LinkState,
    OrderStatus,
    PurchaseFieldError,
    PurchaseRuleError,
    PurchaseStatus,
    QuoteSource,
    Settings,
    VendorInput,
    VendorStatus,
    check_settings,
    mask_iban_display,
)
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    PurchaseOrder,
    PurchaseQuote,
    PurchaseReceipt,
    PurchaseRequest,
    Vendor,
    VendorInvoice,
)
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import purchase_orders as orders_svc
from site_yonetim.services import purchasing as svc
from site_yonetim.services import rate_limit
from site_yonetim.services.files import FileStore

router = APIRouter(prefix="/sites/{slug}", tags=["satın alma"])
public_router = APIRouter(prefix="/public/quote-links", tags=["herkese açık"])
ExpensesRead = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
ExpensesManage = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_MANAGE))]
Paging = Annotated[PageParams, Depends()]

PUBLIC_WINDOW = dt.timedelta(minutes=1)
PUBLIC_GET_LIMIT, PUBLIC_POST_LIMIT = 30, 5
PR_NOT_FOUND = "Satın alma talebi bulunamadı."
ORDER_NOT_FOUND = "Sipariş bulunamadı."
INVOICE_NOT_FOUND = "Fatura bulunamadı."


def _qty(value: Decimal) -> str:
    """Miktar metni: `Decimal("4.500")` → `"4.5"` (para değil; kayan nokta kullanılmaz)."""
    return format_quantity(value)


def _rule(exc: PurchaseRuleError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.fields)


def _form(exc: PurchaseFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def _person(current: CurrentUserDep) -> svc.Person:
    return svc.Person(current.user.id, current.user.full_name)


def _any(ctx: SiteContext, *permissions: Permission) -> None:
    if not any(ctx.access.can(p) for p in permissions):
        raise ForbiddenError


def _buyer(ctx: SiteContext) -> None:
    """Talep açma ve teklif toplama: `requests.assign` ya da `expenses.manage`."""
    _any(ctx, Permission.REQUESTS_ASSIGN, Permission.EXPENSES_MANAGE)


# --- Tedarikçi (62) --------------------------------------------------------------------------


class VendorOut(BaseModel):
    id: uuid.UUID
    name: str
    tax_no: str
    phone: str | None
    email: str | None
    iban_masked: str | None = Field(description="IBAN hiçbir okumada açık dönmez")
    category: str | None
    status: VendorStatus
    note: str | None
    rating_avg: str | None = Field(description="kabuldeki 1–5 puanların ortalaması (`4.5`)")
    rating_count: int
    order_count: int
    created_at: dt.datetime

    @classmethod
    def of(cls, v: Vendor, stats: svc.VendorStats) -> VendorOut:
        return cls(
            id=v.id,
            name=v.name,
            tax_no=v.tax_number,
            phone=v.phone,
            email=v.email,
            iban_masked=mask_iban_display(v.iban),
            category=v.category,
            status=VendorStatus(v.status),
            note=v.note,
            rating_avg=stats.rating_avg,
            rating_count=stats.rating_count,
            order_count=stats.order_count,
            created_at=v.created_at,
        )


class VendorIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    tax_no: str | None = Field(default=None, max_length=20)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    iban: str | None = Field(
        default=None, max_length=40, description="düzenlemede boş/gönderilmezse mevcut korunur"
    )
    category: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=1000)


@router.get("/vendors", summary="Tedarikçiler (unvana göre; sayfalı)")
async def list_vendors(
    ctx: ExpensesRead,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=100, description="unvan ya da vergi no")] = None,
    vendor_status: Annotated[VendorStatus | None, Query(alias="status")] = None,
) -> Page[VendorOut]:
    query = svc.vendors_query(q, vendor_status)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    stats = await svc.vendor_stats(ctx.session, [v.id for v in rows])
    return Page(
        items=[VendorOut.of(v, stats[v.id]) for v in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _save_vendor(ctx: SiteContext, body: VendorIn, vendor: Vendor | None) -> VendorOut:
    data = VendorInput(
        name=body.name,
        tax_no=body.tax_no,
        phone=body.phone,
        email=body.email,
        iban=body.iban,
        category=body.category,
        status=body.status,
        note=body.note,
    )
    try:
        saved = await svc.save_vendor(ctx.session, data, vendor)
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    stats = (await svc.vendor_stats(ctx.session, [saved.id]))[saved.id]
    await ctx.session.flush()
    await ctx.session.refresh(saved)
    out = VendorOut.of(saved, stats)
    await ctx.session.commit()
    return out


@router.post("/vendors", status_code=status.HTTP_201_CREATED, summary="Tedarikçi ekle")
async def create_vendor(body: VendorIn, ctx: ExpensesManage) -> Written[VendorOut]:
    out = await _save_vendor(ctx, body, None)
    return Written(data=out, message=f"{out.name} eklendi.")


@router.patch("/vendors/{vendor_id}", summary="Tedarikçiyi düzenle (boş IBAN korunur)")
async def update_vendor(
    vendor_id: uuid.UUID, body: VendorIn, ctx: ExpensesManage
) -> Written[VendorOut]:
    vendor = await svc.get_vendor(ctx.session, vendor_id, lock=True)
    if vendor is None:
        raise NotFoundError("Tedarikçi bulunamadı.")
    merged = VendorIn(
        name=body.name if body.name is not None else vendor.name,
        tax_no=body.tax_no if body.tax_no is not None else vendor.tax_number,
        phone=body.phone if "phone" in body.model_fields_set else vendor.phone,
        email=body.email if "email" in body.model_fields_set else vendor.email,
        iban=body.iban,
        category=body.category if "category" in body.model_fields_set else vendor.category,
        status=body.status if body.status is not None else vendor.status,
        note=body.note if "note" in body.model_fields_set else vendor.note,
    )
    out = await _save_vendor(ctx, merged, vendor)
    return Written(data=out, message=f"{out.name} güncellendi.")


# --- Ayarlar ve bütçe (63) -------------------------------------------------------------------


class PurchaseThreshold(BaseModel):
    max: Money = Field(description="bu tutarın altındaysa")
    quotes: int = Field(ge=0, le=100)


class PurchaseSettingsOut(BaseModel):
    thresholds: list[PurchaseThreshold]
    above_quotes: int = Field(description="son eşiğin üstünde gereken teklif")
    tolerance_percent: str = Field(description="fatura toplam toleransı, yüzde (`1.00`)")
    link_days: int = Field(description="tedarikçi teklif linki geçerliliği (gün)")

    @classmethod
    def of(cls, s: Settings) -> PurchaseSettingsOut:
        return cls(
            thresholds=[PurchaseThreshold(max=t.max, quotes=t.quotes) for t in s.thresholds],
            above_quotes=s.above_quotes,
            tolerance_percent=f"{s.tolerance_percent:.2f}",
            link_days=s.link_days,
        )


class PurchaseSettingsIn(BaseModel):
    thresholds: list[PurchaseThreshold] = Field(max_length=20)
    above_quotes: int = Field(ge=0, le=100)
    tolerance_percent: Decimal = Field(ge=0, le=100, max_digits=6, decimal_places=2)
    link_days: int = Field(ge=0, le=365)


@router.get("/purchase-settings", summary="Satın alma ayarları (yoksa varsayılan)")
async def get_settings(ctx: ExpensesRead) -> PurchaseSettingsOut:
    return PurchaseSettingsOut.of(await svc.read_settings(ctx.session))


@router.put("/purchase-settings", summary="Satın alma ayarlarını kaydet")
async def put_settings(
    body: PurchaseSettingsIn, ctx: ExpensesManage
) -> Written[PurchaseSettingsOut]:
    try:
        checked = check_settings(
            [(t.max, t.quotes) for t in body.thresholds],
            body.above_quotes,
            body.tolerance_percent,
            body.link_days,
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    saved = await svc.save_settings(ctx.session, checked)
    await ctx.session.commit()
    return Written(data=PurchaseSettingsOut.of(saved), message="Satın alma ayarları kaydedildi.")


class PurchaseBudgetOut(BaseModel):
    has_budget: bool = Field(description="false → kategoride işletme projesi kalemi yok")
    annual: Money
    spent: Money = Field(description="bu yılın gerçekleşen gideri")
    committed: Money = Field(description="onay bekleyen/onaylı talepler + açık siparişler")
    remaining: Money


@router.get("/purchase-budget", summary="Kategorinin kalan bütçesi")
async def purchase_budget(
    ctx: ExpensesRead,
    today: TodayDep,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PurchaseBudgetOut:
    if category_id is None or await svc.category(ctx.session, category_id) is None:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {"category_id": "Kategori seçin."},
        )
    b = await svc.budget(ctx.session, category_id, today=today)
    return PurchaseBudgetOut(
        has_budget=b.has_budget,
        annual=b.annual,
        spent=b.spent,
        committed=b.committed,
        remaining=b.remaining,
    )


# --- Talep, teklif, link, onay (64) ----------------------------------------------------------


class PurchaseItemIn(BaseModel):
    description: str | None = Field(default=None, max_length=300)
    qty: Decimal | str = Field(description="miktar (en çok 3 ondalık)")
    unit: str | None = Field(default=None, max_length=40)
    est_unit_price: Money | None = None
    stock_item_id: uuid.UUID | None = Field(
        default=None, description="stoklu kalem: kabulde stoğa girer"
    )


class PurchaseRequestIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=1000)
    category_id: uuid.UUID | None = None
    items: list[PurchaseItemIn] = Field(default_factory=list, max_length=60)


class PurchaseItemOut(BaseModel):
    description: str
    qty: str
    unit: str
    est_unit_price: Money
    stock_item_id: uuid.UUID | None
    stock_item_name: str | None


class PurchaseBudgetSnapshot(BaseModel):
    annual: Money
    spent: Money
    committed: Money
    remaining: Money


class PurchaseUnitPrice(BaseModel):
    unit_price: Money


class PurchaseQuoteOut(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str
    lines: list[PurchaseUnitPrice]
    total: Money
    delivery_days: int
    payment_terms: str
    valid_until: dt.date
    note: str | None
    file_id: uuid.UUID | None = Field(description="belge: GET …/files/{id}")
    file_name: str | None
    source: QuoteSource = Field(description='vendor_link → "Tedarikçi girdi"')
    created_by: str
    created_at: dt.datetime
    rating_avg: str | None
    rating_count: int


class QuoteLinkOut(BaseModel):
    id: uuid.UUID = Field(description="belirteç yalnız oluşturulurken bir kez döner")
    vendor_name: str
    expires_at: dt.datetime
    used_at: dt.datetime | None
    state: LinkState


class PurchaseDecisionOut(BaseModel):
    stage: DecisionStage
    decision: DecisionKind
    by: str
    at: dt.datetime
    note: str | None


class PurchaseApprovalRef(BaseModel):
    id: uuid.UUID
    number: int
    action_type: str
    status: ApprovalStatus


class PurchaseRequestOut(BaseModel):
    id: uuid.UUID
    number: str
    title: str
    reason: str
    category_id: uuid.UUID
    category_name: str
    items: list[PurchaseItemOut]
    estimated_total: Money
    budget: PurchaseBudgetSnapshot | None = Field(description="talep anındaki bütçe görüntüsü")
    over_budget: bool
    required_quotes: int
    status: PurchaseStatus
    selected_quote_id: uuid.UUID | None
    exception_reason: str | None
    decisions: list[PurchaseDecisionOut]
    reject_reason: str | None
    created_by: str
    created_by_id: uuid.UUID | None
    created_at: dt.datetime
    quotes: list[PurchaseQuoteOut]
    quote_count: int
    links: list[QuoteLinkOut]
    order_id: uuid.UUID | None
    order_number: str | None
    approval: PurchaseApprovalRef | None = Field(
        description="açık onay talebi (onay kuralı varsa) — karar Onaylar ekranından da verilir"
    )

    @classmethod
    def of(cls, v: svc.RequestView) -> PurchaseRequestOut:
        pr = v.request
        snapshot = None
        if pr.budget_annual is not None:
            spent = pr.budget_spent or Decimal(0)
            committed = pr.budget_committed or Decimal(0)
            snapshot = PurchaseBudgetSnapshot(
                annual=pr.budget_annual,
                spent=spent,
                committed=committed,
                remaining=pr.budget_annual - spent - committed,
            )
        return cls(
            id=pr.id,
            number=pr.number,
            title=pr.title,
            reason=pr.reason,
            category_id=pr.expense_category_id,
            category_name=v.category_name,
            items=[
                PurchaseItemOut(
                    description=i.description,
                    qty=_qty(i.quantity),
                    unit=i.unit,
                    est_unit_price=i.est_unit_price,
                    stock_item_id=i.stock_item_id,
                    stock_item_name=v.stock_names.get(i.stock_item_id) if i.stock_item_id else None,
                )
                for i in v.items
            ],
            estimated_total=pr.estimated_total,
            budget=snapshot,
            over_budget=pr.over_budget,
            required_quotes=pr.required_quotes,
            status=PurchaseStatus(pr.status),
            selected_quote_id=pr.selected_quote_id,
            exception_reason=pr.exception_reason,
            decisions=[
                PurchaseDecisionOut(
                    stage=DecisionStage(d.stage),
                    decision=DecisionKind(d.decision),
                    by=d.by_name,
                    at=d.created_at,
                    note=d.note,
                )
                for d in v.decisions
            ],
            reject_reason=pr.reject_reason,
            created_by=pr.created_by_name,
            created_by_id=pr.created_by,
            created_at=pr.created_at,
            quotes=[_quote_out(q) for q in v.quotes],
            quote_count=len(v.quotes),
            links=[
                QuoteLinkOut(
                    id=link.link.id,
                    vendor_name=link.vendor_name,
                    expires_at=link.link.expires_at,
                    used_at=link.link.used_at,
                    state=link.state,
                )
                for link in v.links
            ],
            order_id=v.order_id,
            order_number=v.order_number,
            approval=(
                PurchaseApprovalRef(
                    id=v.approval.id,
                    number=v.approval.number,
                    action_type=v.approval.action_type,
                    status=ApprovalStatus(v.approval.status),
                )
                if v.approval
                else None
            ),
        )


def _quote_out(q: svc.QuoteView) -> PurchaseQuoteOut:
    return PurchaseQuoteOut(
        id=q.quote.id,
        vendor_id=q.quote.vendor_id,
        vendor_name=q.vendor_name,
        lines=[PurchaseUnitPrice(unit_price=p) for p in q.unit_prices],
        total=q.quote.total,
        delivery_days=q.quote.delivery_days,
        payment_terms=q.quote.payment_terms,
        valid_until=q.quote.valid_until,
        note=q.quote.note,
        file_id=q.quote.stored_file_id,
        file_name=q.file_name,
        source=QuoteSource(q.quote.source),
        created_by=q.quote.created_by_name,
        created_at=q.quote.created_at,
        rating_avg=q.stats.rating_avg,
        rating_count=q.stats.rating_count,
    )


class PurchaseRequestRow(BaseModel):
    id: uuid.UUID
    number: str
    title: str
    category_id: uuid.UUID
    category_name: str
    over_budget: bool
    estimated_total: Money
    quote_count: int
    required_quotes: int
    status: PurchaseStatus
    order_id: uuid.UUID | None
    order_number: str | None
    created_by: str
    created_at: dt.datetime


async def _pr(ctx: SiteContext, pr_id: uuid.UUID, *, lock: bool = False) -> PurchaseRequest:
    pr = await svc.get_request(ctx.session, pr_id, lock=lock)
    if pr is None:
        raise NotFoundError(PR_NOT_FOUND)
    return pr


async def _pr_out(ctx: SiteContext, pr: PurchaseRequest, now: dt.datetime) -> PurchaseRequestOut:
    await ctx.session.flush()
    await ctx.session.refresh(pr)
    return PurchaseRequestOut.of(await svc.load_request(ctx.session, pr, now=now))


@router.get("/purchase-requests", summary="Satın alma talepleri (yeni üstte; sayfalı)")
async def list_requests(
    ctx: ExpensesRead,
    paging: Paging,
    request_status: Annotated[PurchaseStatus | None, Query(alias="status")] = None,
) -> Page[PurchaseRequestRow]:
    query = svc.requests_query(request_status)
    total = await ctx.session.scalar(
        select(func.count()).select_from(
            select(PurchaseRequest.id)
            .where(*([PurchaseRequest.status == request_status.value] if request_status else []))
            .subquery()
        )
    )
    rows = (await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size))).all()
    return Page(
        items=[
            PurchaseRequestRow(
                id=pr.id,
                number=pr.number,
                title=pr.title,
                category_id=pr.expense_category_id,
                category_name=category_name,
                over_budget=pr.over_budget,
                estimated_total=pr.estimated_total,
                quote_count=int(count or 0),
                required_quotes=pr.required_quotes,
                status=PurchaseStatus(pr.status),
                order_id=order_id,
                order_number=order_number,
                created_by=pr.created_by_name,
                created_at=pr.created_at,
            )
            for pr, category_name, count, order_id, order_number in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.get("/purchase-requests/{pr_id}", summary="Talep: kalemler, teklifler, linkler, kararlar")
async def get_request(pr_id: uuid.UUID, ctx: ExpensesRead, now: NowDep) -> PurchaseRequestOut:
    return PurchaseRequestOut.of(
        await svc.load_request(ctx.session, await _pr(ctx, pr_id), now=now)
    )


@router.post(
    "/purchase-requests", status_code=status.HTTP_201_CREATED, summary="Satın alma talebi aç"
)
async def create_request(
    body: PurchaseRequestIn,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[PurchaseRequestOut]:
    _buyer(ctx)
    data = svc.RequestInput(
        title=body.title,
        reason=body.reason,
        category_id=body.category_id,
        items=[
            svc.ItemInput(i.description, i.qty, i.unit, i.est_unit_price, i.stock_item_id)
            for i in body.items
        ],
    )
    try:
        pr = await svc.create_request(ctx.session, data, actor=_person(current), today=today)
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    out = await _pr_out(ctx, pr, now)
    await ctx.session.commit()
    over = " Bütçe aşımı işaretlendi." if pr.over_budget else ""
    return Written(
        data=out, message=f"{pr.number} açıldı; {pr.required_quotes} teklif gerekiyor.{over}"
    )


class PurchaseQuotePriceIn(BaseModel):
    unit_price: Money | None = None


class PurchaseQuoteIn(BaseModel):
    vendor_id: uuid.UUID | None = None
    lines: list[PurchaseQuotePriceIn] = Field(default_factory=list, max_length=60)
    delivery_days: int | None = Field(default=None, ge=-1, le=10000)
    payment_terms: str | None = Field(default=None, max_length=200)
    valid_until: dt.date | None = None
    note: str | None = Field(default=None, max_length=1000)


def _quote_input(body: PurchaseQuoteIn | PublicQuoteIn) -> svc.QuoteInput:
    return svc.QuoteInput(
        unit_prices=[line.unit_price for line in body.lines],
        delivery_days=body.delivery_days,
        payment_terms=body.payment_terms,
        valid_until=body.valid_until,
        note=body.note,
    )


@router.post(
    "/purchase-requests/{pr_id}/quotes",
    status_code=status.HTTP_201_CREATED,
    summary="Teklifi personel girer",
)
async def add_quote(
    pr_id: uuid.UUID,
    body: PurchaseQuoteIn,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[PurchaseQuoteOut]:
    _buyer(ctx)
    pr = await _pr(ctx, pr_id, lock=True)
    try:
        quote = await svc.add_quote(
            ctx.session,
            pr,
            body.vendor_id,
            _quote_input(body),
            actor=_person(current),
            today=today,
            now=now,
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    view = await svc.load_request(ctx.session, pr, now=now)
    out = next(_quote_out(q) for q in view.quotes if q.quote.id == quote.id)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"{out.vendor_name} teklifi eklendi ({len(view.quotes)}/{pr.required_quotes}).",
    )


class QuoteLinkIn(BaseModel):
    vendor_id: uuid.UUID | None = None


class QuoteLinkCreated(BaseModel):
    token: str = Field(description="yalnız bu yanıtta; tedarikçiye /teklif/{token} iletilir")
    expires_at: dt.datetime
    vendor_name: str
    vendor_email: str | None


@router.post(
    "/purchase-requests/{pr_id}/quote-links",
    status_code=status.HTTP_201_CREATED,
    summary="Tedarikçiye tek kullanımlık teklif linki",
)
async def create_link(
    pr_id: uuid.UUID, body: QuoteLinkIn, ctx: ExpensesRead, current: CurrentUserDep, now: NowDep
) -> Written[QuoteLinkCreated]:
    _buyer(ctx)
    pr = await _pr(ctx, pr_id, lock=True)
    try:
        created = await svc.create_link(
            ctx.session, pr, body.vendor_id, actor=_person(current), now=now
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.commit()
    vendor = created.vendor
    mail = (
        f" E-posta gönderimi henüz yok (K5); linki {vendor.email} adresine siz iletin."
        if vendor.email
        else " Linki kopyalayıp tedarikçiye iletin."
    )
    return Written(
        data=QuoteLinkCreated(
            token=created.token,
            expires_at=created.link.expires_at,
            vendor_name=vendor.name,
            vendor_email=vendor.email,
        ),
        message=(
            f"{vendor.name} için teklif linki hazır; {created.days} gün geçerli, tek kullanımlık."
            + mail
        ),
    )


class SelectQuoteIn(BaseModel):
    quote_id: uuid.UUID | None = None
    exception_reason: str | None = Field(default=None, max_length=1000)


@router.post("/purchase-requests/{pr_id}/select-quote", summary="Teklifi seç ve onaya gönder")
async def select_quote(
    pr_id: uuid.UUID,
    body: SelectQuoteIn,
    ctx: ExpensesRead,
    today: TodayDep,
    now: NowDep,
) -> Written[PurchaseRequestOut]:
    _buyer(ctx)
    pr = await _pr(ctx, pr_id, lock=True)
    try:
        await svc.select_quote(
            ctx.session, ctx.site, pr, body.quote_id, body.exception_reason, today=today, now=now
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    except approvals_svc.ApprovalError as exc:  # pragma: no cover — yürütücü kayıtlı
        raise ApiError(exc.status, exc.code, exc.message) from exc
    out = await _pr_out(ctx, pr, now)
    await ctx.session.commit()
    via = " (onay kuralına göre Onaylar ekranına düştü)" if out.approval else ""
    message = (
        f"Teklif istisnası onaya gönderildi{via}."
        if out.status is PurchaseStatus.EXCEPTION_PENDING
        else f"Teklif seçildi; satın alma onayı bekleniyor{via}."
    )
    return Written(data=out, message=message)


class PurchaseDecideIn(BaseModel):
    decision: Literal["approve", "reject"] | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/purchase-requests/{pr_id}/decide", summary="İstisna / satın alma kararı")
async def decide_request(
    pr_id: uuid.UUID,
    body: PurchaseDecideIn,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[PurchaseRequestOut]:
    pr = await _pr(ctx, pr_id, lock=True)
    try:
        message = await svc.decide(
            ctx.session,
            ctx.site,
            pr,
            body.decision or "",
            body.note,
            actor=_person(current),
            can_manage=ctx.access.can(Permission.EXPENSES_MANAGE),
            today=today,
            now=now,
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _pr_out(ctx, pr, now)
    await ctx.session.commit()
    return Written(data=out, message=message)


@router.post("/purchase-requests/{pr_id}/cancel", summary="Talebi iptal et (açık linkler kapanır)")
async def cancel_request(
    pr_id: uuid.UUID, ctx: ExpensesRead, current: CurrentUserDep, now: NowDep
) -> Written[PurchaseRequestOut]:
    _buyer(ctx)
    pr = await _pr(ctx, pr_id, lock=True)
    try:
        await svc.cancel(
            ctx.session,
            pr,
            actor=_person(current),
            can_manage=ctx.access.can(Permission.EXPENSES_MANAGE),
            now=now,
        )
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _pr_out(ctx, pr, now)
    await ctx.session.commit()
    return Written(data=out, message="Talep iptal edildi; açık teklif linkleri kapatıldı.")


# --- Sipariş ve kabul (65) -------------------------------------------------------------------


class PurchaseOrderLineOut(BaseModel):
    id: uuid.UUID
    description: str
    qty: str
    unit: str
    unit_price: Money
    total: Money
    stock_item_id: uuid.UUID | None
    received: str
    invoiced: str = Field(description="itiraz edilmemiş faturalardaki miktar")


class PurchaseReceiptQty(BaseModel):
    qty: str


class PurchaseReceiptOut(BaseModel):
    id: uuid.UUID
    lines: list[PurchaseReceiptQty]
    note: str | None
    rating: int | None
    file_id: uuid.UUID | None
    file_name: str | None
    by: str
    at: dt.datetime


class VendorInvoiceLineOut(BaseModel):
    qty: str
    unit_price: Money
    total: Money


class VendorInvoiceDecisionOut(BaseModel):
    by: str
    at: dt.datetime
    note: str | None


class VendorInvoiceOut(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    order_number: str
    vendor_id: uuid.UUID
    vendor_name: str
    number: str
    date: dt.date
    due_date: dt.date
    lines: list[VendorInvoiceLineOut]
    total: Money
    expected_total: Money
    diffs: list[str]
    status: InvoiceStatus
    decision: VendorInvoiceDecisionOut | None
    approval_request_id: uuid.UUID | None = Field(description="fark onay kuralına düştüyse")
    file_id: uuid.UUID | None
    file_name: str | None
    paid_on: dt.date | None
    cash_account_id: uuid.UUID | None
    expense_id: uuid.UUID | None = Field(description='"ödendi" ile yazılan gider')
    overdue: bool
    created_by: str
    created_by_id: uuid.UUID | None
    created_at: dt.datetime

    @classmethod
    def of(cls, v: orders_svc.InvoiceView, today: dt.date) -> VendorInvoiceOut:
        i = v.invoice
        return cls(
            id=i.id,
            order_id=i.order_id,
            order_number=v.order_number,
            vendor_id=i.vendor_id,
            vendor_name=v.vendor_name,
            number=i.number,
            date=i.invoice_date,
            due_date=i.due_date,
            lines=[
                VendorInvoiceLineOut(
                    qty=_qty(x.quantity), unit_price=x.unit_price, total=x.line_total
                )
                for x in v.lines
            ],
            total=i.total,
            expected_total=i.expected_total,
            diffs=list(i.diffs),
            status=InvoiceStatus(i.status),
            decision=(
                VendorInvoiceDecisionOut(
                    by=i.decided_by_name, at=i.decided_at, note=i.decision_note
                )
                if i.decided_at and i.decided_by_name
                else None
            ),
            approval_request_id=i.approval_request_id,
            file_id=i.stored_file_id,
            file_name=v.file_name,
            paid_on=i.paid_on,
            cash_account_id=i.cash_account_id,
            expense_id=i.expense_id,
            overdue=i.status == InvoiceStatus.MATCHED.value and i.due_date < today,
            created_by=i.created_by_name,
            created_by_id=i.created_by,
            created_at=i.created_at,
        )


class PurchaseOrderOut(BaseModel):
    id: uuid.UUID
    number: str
    pr_id: uuid.UUID
    pr_number: str
    vendor_id: uuid.UUID
    vendor_name: str
    category_id: uuid.UUID
    lines: list[PurchaseOrderLineOut]
    total: Money
    status: OrderStatus
    created_by: str
    created_at: dt.datetime
    receipts: list[PurchaseReceiptOut]
    invoices: list[VendorInvoiceOut]


class PurchaseOrderRow(BaseModel):
    id: uuid.UUID
    number: str
    pr_id: uuid.UUID
    pr_number: str
    vendor_name: str
    total: Money
    status: OrderStatus
    created_at: dt.datetime


async def _order(ctx: SiteContext, order_id: uuid.UUID, *, lock: bool = False) -> PurchaseOrder:
    order = await orders_svc.get_order(ctx.session, order_id, lock=lock)
    if order is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    return order


async def _order_out(ctx: SiteContext, order: PurchaseOrder, today: dt.date) -> PurchaseOrderOut:
    session = ctx.session
    await session.flush()
    await session.refresh(order)
    lines = await orders_svc.order_lines(session, order.id)
    for line in lines:
        await session.refresh(line)
    invoiced = await orders_svc.invoiced_quantities(session, order.id)
    receipts = await orders_svc.receipts_of(session, order.id)
    quantities = await orders_svc.receipt_quantities(session, [r.id for r in receipts])
    names = await svc.file_names(session, [r.stored_file_id for r in receipts])
    pr_number = await session.scalar(
        select(PurchaseRequest.number).where(PurchaseRequest.id == order.request_id)
    )
    vendor_name = await session.scalar(select(Vendor.name).where(Vendor.id == order.vendor_id))
    invoice_rows = list(
        (
            await session.execute(
                orders_svc.invoices_query(None).where(VendorInvoice.order_id == order.id)
            )
        ).all()
    )
    invoices = await orders_svc.invoice_views(session, list(reversed(invoice_rows)))
    return PurchaseOrderOut(
        id=order.id,
        number=order.number,
        pr_id=order.request_id,
        pr_number=pr_number or "",
        vendor_id=order.vendor_id,
        vendor_name=vendor_name or "",
        category_id=order.expense_category_id,
        lines=[
            PurchaseOrderLineOut(
                id=line.id,
                description=line.description,
                qty=_qty(line.quantity),
                unit=line.unit,
                unit_price=line.unit_price,
                total=line.line_total,
                stock_item_id=line.stock_item_id,
                received=_qty(line.received_quantity),
                invoiced=_qty(invoiced.get(line.id, Decimal(0))),
            )
            for line in lines
        ],
        total=order.total,
        status=OrderStatus(order.status),
        created_by=order.created_by_name,
        created_at=order.created_at,
        receipts=[
            PurchaseReceiptOut(
                id=r.id,
                lines=[
                    PurchaseReceiptQty(qty=_qty(quantities.get((r.id, line.id), Decimal(0))))
                    for line in lines
                ],
                note=r.note,
                rating=r.rating,
                file_id=r.stored_file_id,
                file_name=names.get(r.stored_file_id) if r.stored_file_id else None,
                by=r.by_name,
                at=r.created_at,
            )
            for r in receipts
        ],
        invoices=[VendorInvoiceOut.of(v, today) for v in invoices],
    )


@router.get("/purchase-orders", summary="Siparişler (yeni üstte; sayfalı)")
async def list_orders(
    ctx: ExpensesRead,
    paging: Paging,
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
) -> Page[PurchaseOrderRow]:
    query = orders_svc.orders_query(order_status)
    total = await ctx.session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size))).all()
    return Page(
        items=[
            PurchaseOrderRow(
                id=o.id,
                number=o.number,
                pr_id=o.request_id,
                pr_number=pr_number,
                vendor_name=vendor_name,
                total=o.total,
                status=OrderStatus(o.status),
                created_at=o.created_at,
            )
            for o, vendor_name, pr_number in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.get("/purchase-orders/{order_id}", summary="Sipariş: kalemler, kabuller, faturalar")
async def get_order(order_id: uuid.UUID, ctx: ExpensesRead, today: TodayDep) -> PurchaseOrderOut:
    return await _order_out(ctx, await _order(ctx, order_id), today)


class PurchaseOrderIn(BaseModel):
    pr_id: uuid.UUID


class PurchaseOrderWriteOut(BaseModel):
    status: Literal["created", "approval_pending"]
    order: PurchaseOrderOut | None
    approval_request_id: uuid.UUID | None


@router.post(
    "/purchase-orders",
    status_code=status.HTTP_201_CREATED,
    summary='Onaylı talepten sipariş ("Sipariş" onay kuralı varsa 202)',
)
async def create_order(
    body: PurchaseOrderIn,
    ctx: ExpensesManage,
    current: CurrentUserDep,
    response: Response,
    today: TodayDep,
    now: NowDep,
) -> Written[PurchaseOrderWriteOut]:
    pr = await _pr(ctx, body.pr_id, lock=True)
    try:
        result = await orders_svc.create_order(
            ctx.session, ctx.site, pr, actor=_person(current), today=today, now=now
        )
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    if result.approval is not None:
        await ctx.session.commit()
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=PurchaseOrderWriteOut(
                status="approval_pending", order=None, approval_request_id=result.approval.id
            ),
            message="Sipariş onaya gönderildi; onaylanınca numaralı sipariş oluşur.",
        )
    if result.order is None:  # pragma: no cover
        raise RuntimeError("Sipariş yazılmadı")
    out = await _order_out(ctx, result.order, today)
    await ctx.session.commit()
    mail = (
        f" ({out.vendor_name} için e-posta gönderimi henüz yok — K5; sipariş formunu yazdırıp "
        "iletin)"
    )
    return Written(
        data=PurchaseOrderWriteOut(status="created", order=out, approval_request_id=None),
        message=f"{out.number} oluşturuldu{mail}.",
    )


class PurchaseReceiptQtyIn(BaseModel):
    qty: Decimal | str = Decimal(0)


class PurchaseReceiptIn(BaseModel):
    lines: list[PurchaseReceiptQtyIn] = Field(default_factory=list, max_length=60)
    note: str | None = Field(default=None, max_length=1000)
    rating: int | None = Field(default=None, ge=-100, le=100)


@router.post(
    "/purchase-orders/{order_id}/receipts",
    status_code=status.HTTP_201_CREATED,
    summary="Mal/hizmet kabul (stoklu kalem sistem adına stoğa girer)",
)
async def receive(
    order_id: uuid.UUID,
    body: PurchaseReceiptIn,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[PurchaseOrderOut]:
    _any(ctx, Permission.REQUESTS_ASSIGN, Permission.INVENTORY_MANAGE, Permission.EXPENSES_MANAGE)
    order = await _order(ctx, order_id, lock=True)
    try:
        _, stocked = await orders_svc.receive(
            ctx.session,
            order,
            [line.qty for line in body.lines],
            note=body.note,
            rating=body.rating,
            actor=_person(current),
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _order_out(ctx, order, today)
    await ctx.session.commit()
    note = f" Stoklu kalemler stoğa girdi ({stocked})." if stocked else ""
    whole = "Tamamı" if out.status in (OrderStatus.RECEIVED, OrderStatus.CLOSED) else "Kısmi"
    return Written(data=out, message=f"{whole} kabul kaydedildi.{note}")


# --- Tedarikçi faturası ve üçlü eşleşme (66) -------------------------------------------------


class VendorInvoiceLineIn(BaseModel):
    qty: Decimal | str = Decimal(0)
    unit_price: Money | None = None


class VendorInvoiceIn(BaseModel):
    order_id: uuid.UUID | None = None
    number: str | None = Field(default=None, max_length=100)
    date: dt.date | None = None
    due_date: dt.date | None = None
    lines: list[VendorInvoiceLineIn] = Field(default_factory=list, max_length=60)


async def _invoice_out(
    ctx: SiteContext, invoice: VendorInvoice, today: dt.date
) -> VendorInvoiceOut:
    await ctx.session.flush()
    await ctx.session.refresh(invoice)
    rows = list(
        (
            await ctx.session.execute(
                orders_svc.invoices_query(None).where(VendorInvoice.id == invoice.id)
            )
        ).all()
    )
    return VendorInvoiceOut.of((await orders_svc.invoice_views(ctx.session, rows))[0], today)


async def _invoice(ctx: SiteContext, invoice_id: uuid.UUID, *, lock: bool = False) -> VendorInvoice:
    invoice = await orders_svc.get_invoice(ctx.session, invoice_id, lock=lock)
    if invoice is None:
        raise NotFoundError(INVOICE_NOT_FOUND)
    return invoice


@router.get("/vendor-invoices", summary="Tedarikçi faturaları (yeni üstte; sayfalı)")
async def list_invoices(
    ctx: ExpensesRead,
    paging: Paging,
    today: TodayDep,
    invoice_status: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
) -> Page[VendorInvoiceOut]:
    query = orders_svc.invoices_query(invoice_status)
    total = await ctx.session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = list(
        (await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size))).all()
    )
    views = await orders_svc.invoice_views(ctx.session, rows)
    return Page(
        items=[VendorInvoiceOut.of(v, today) for v in views],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.post(
    "/vendor-invoices",
    status_code=status.HTTP_201_CREATED,
    summary="Tedarikçi faturası: sipariş + kabul + fatura eşleşmesi",
)
async def create_invoice(
    body: VendorInvoiceIn,
    ctx: ExpensesManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[VendorInvoiceOut]:
    data = orders_svc.InvoiceInput(
        order_id=body.order_id,
        number=body.number,
        invoice_date=body.date,
        due_date=body.due_date,
        lines=[(line.qty, line.unit_price) for line in body.lines],
    )
    try:
        invoice = await orders_svc.create_invoice(
            ctx.session, ctx.site, data, actor=_person(current), today=today, now=now
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _invoice_out(ctx, invoice, today)
    await ctx.session.commit()
    if out.status is InvoiceStatus.MATCHED:
        message = f"Fatura eşleşti; ödenecekler listesine {out.due_date:%d.%m.%Y} vadesiyle girdi."
    else:
        via = " (onay kuralına göre Onaylar ekranına düştü)" if out.approval_request_id else ""
        message = (
            "Fatura sipariş/kabulle eşleşmedi; fark onaya düştü, ödenecekler listesine "
            f"giremez{via}."
        )
    return Written(data=out, message=message)


class InvoiceDecideIn(BaseModel):
    decision: Literal["approve", "reject"] | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/vendor-invoices/{invoice_id}/decide", summary="Fatura farkını onayla / itiraz et")
async def decide_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceDecideIn,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[VendorInvoiceOut]:
    invoice = await _invoice(ctx, invoice_id, lock=True)
    try:
        message = await orders_svc.decide_invoice(
            ctx.session,
            ctx.site,
            invoice,
            body.decision or "",
            body.note,
            actor=_person(current),
            can_manage=ctx.access.can(Permission.EXPENSES_MANAGE),
            now=now,
        )
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _invoice_out(ctx, invoice, today)
    await ctx.session.commit()
    return Written(data=out, message=message)


# --- Ödenecekler (67) ------------------------------------------------------------------------


class DirectExpensesOut(BaseModel):
    count: int
    total: Money


class PayablesOut(BaseModel):
    invoices: list[VendorInvoiceOut] = Field(description="eşleşmiş, ödenmemiş; vadeye göre")
    direct_expenses: DirectExpensesOut = Field(description="siparişsiz ödenmemiş giderler")
    total: Money


@router.get("/payables", summary="Ödenecekler: eşleşmiş faturalar + siparişsiz gider özeti")
async def payables(ctx: ExpensesRead, today: TodayDep) -> PayablesOut:
    result = await orders_svc.payables(ctx.session)
    return PayablesOut(
        invoices=[VendorInvoiceOut.of(v, today) for v in result.invoices],
        direct_expenses=DirectExpensesOut(count=result.direct_count, total=result.direct_total),
        total=result.total,
    )


class MarkPaidIn(BaseModel):
    paid_on: dt.date
    cash_account_id: uuid.UUID


@router.post(
    "/vendor-invoices/{invoice_id}/mark-paid",
    summary="Ödendi: gider + kasa çıkışı aynı işlemde (Idempotency-Key)",
    response_model=Written[VendorInvoiceOut],
)
async def mark_paid(
    invoice_id: uuid.UUID,
    body: MarkPaidIn,
    ctx: ExpensesManage,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[VendorInvoiceOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        invoice = await _invoice(ctx, invoice_id, lock=True)
        await orders_svc.mark_paid(
            ctx.session,
            invoice,
            paid_on=body.paid_on,
            cash_account_id=body.cash_account_id,
            today=today,
            actor=_person(current),
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except PurchaseFieldError as exc:
        raise _form(exc) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    out = await _invoice_out(ctx, invoice, today)
    result = Written(
        data=out,
        message=(
            f"{out.vendor_name} {out.number} ödendi olarak işaretlendi; "
            f"{format_money_tr(out.total)} gider yazıldı ve hesaptan düştü."
        ),
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result


# --- Belge ekleme --------------------------------------------------------------------------


class PurchaseFileOut(BaseModel):
    id: uuid.UUID = Field(description="indirme: GET …/files/{id}")
    file_name: str


async def _attach(
    ctx: SiteContext,
    target: PurchaseQuote | PurchaseReceipt | VendorInvoice,
    file: UploadFile | None,
    uploaded_by: str,
    store: FileStore,
) -> Written[PurchaseFileOut]:
    written: str | None = None
    try:
        data = await file.read(MAX_FILE_BYTES + 1) if file is not None else b""
        attached = await orders_svc.attach_file(
            ctx.session,
            target,
            file_name=file.filename if file else None,
            data=data,
            uploaded_by=uploaded_by,
            store=store,
        )
        written = attached.written_path
        out = PurchaseFileOut(id=attached.file.id, file_name=attached.file.file_name)
        await ctx.session.commit()
    except FinanceRuleError as exc:
        if written:
            store.delete(written)
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, {"file": exc.message}
        ) from exc
    except PurchaseRuleError as exc:
        raise _rule(exc) from exc
    except BaseException:
        if written:
            store.delete(written)
        raise
    return Written(data=out, message=f"{out.file_name} eklendi.")


FileField = Annotated[
    UploadFile | None, File(description="PDF, JPG, PNG, WEBP · ≤ 10 MB (içerik doğrulanır)")
]


@router.post(
    "/purchase-quotes/{quote_id}/file",
    status_code=status.HTTP_201_CREATED,
    summary="Teklif belgesi ekle (multipart, bir kez)",
)
async def attach_quote_file(
    quote_id: uuid.UUID,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    store: StoreDep,
    file: FileField = None,
) -> Written[PurchaseFileOut]:
    _buyer(ctx)
    quote = await ctx.session.scalar(
        select(PurchaseQuote).where(PurchaseQuote.id == quote_id).with_for_update()
    )
    if quote is None:
        raise NotFoundError("Teklif bulunamadı.")
    return await _attach(ctx, quote, file, current.user.full_name, store)


@router.post(
    "/purchase-receipts/{receipt_id}/file",
    status_code=status.HTTP_201_CREATED,
    summary="Kabul fotoğrafı / irsaliye ekle (multipart, bir kez)",
)
async def attach_receipt_file(
    receipt_id: uuid.UUID,
    ctx: ExpensesRead,
    current: CurrentUserDep,
    store: StoreDep,
    file: FileField = None,
) -> Written[PurchaseFileOut]:
    _any(ctx, Permission.REQUESTS_ASSIGN, Permission.INVENTORY_MANAGE, Permission.EXPENSES_MANAGE)
    receipt = await orders_svc.get_receipt(ctx.session, receipt_id, lock=True)
    if receipt is None:
        raise NotFoundError("Kabul kaydı bulunamadı.")
    return await _attach(ctx, receipt, file, current.user.full_name, store)


@router.post(
    "/vendor-invoices/{invoice_id}/file",
    status_code=status.HTTP_201_CREATED,
    summary="Fatura görüntüsü ekle (multipart, bir kez; ödenince gidere bağlanır)",
)
async def attach_invoice_file(
    invoice_id: uuid.UUID,
    ctx: ExpensesManage,
    current: CurrentUserDep,
    store: StoreDep,
    file: FileField = None,
) -> Written[PurchaseFileOut]:
    invoice = await _invoice(ctx, invoice_id, lock=True)
    if invoice.status == InvoiceStatus.PAID.value:
        raise ApiError(HTTPStatus.CONFLICT, "invalid_status", "Ödenmiş faturaya belge eklenemez.")
    return await _attach(ctx, invoice, file, current.user.full_name, store)


# --- Herkese açık: tedarikçi teklif formu ----------------------------------------------------


async def _limit(
    factory: FactoryDep, request: Request, now: dt.datetime, kind: str, limit: int
) -> None:
    ip = request.client.host if request.client else "unknown"
    allowed = await rate_limit.hit(
        factory, key=f"quote-link-{kind}:{ip}", now=now, limit=limit, window=PUBLIC_WINDOW
    )
    if not allowed:
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_requests",
            "Çok fazla deneme yaptınız, biraz sonra tekrar deneyin.",
            headers={"Retry-After": str(int(PUBLIC_WINDOW.total_seconds()))},
        )


def _link_not_found() -> ApiError:
    return ApiError(HTTPStatus.NOT_FOUND, "not_found", "Bağlantı bulunamadı.")


class PublicQuoteItemOut(BaseModel):
    description: str
    qty: str
    unit: str


class PublicQuoteLinkOut(BaseModel):
    site_name: str
    vendor_name: str
    number: str
    title: str
    items: list[PublicQuoteItemOut] = Field(
        description="yalnız açıklama, miktar, birim — tahmini fiyat, bütçe, diğer teklifler yok"
    )
    expires_at: dt.datetime


@public_router.get("/{token}", summary="Tedarikçi teklif formu (oturumsuz, tek kullanımlık)")
async def public_link(
    token: str, request: Request, factory: FactoryDep, now: NowDep
) -> PublicQuoteLinkOut:
    await _limit(factory, request, now, "get", PUBLIC_GET_LIMIT)
    site_id = await svc.site_of_token(factory, token)
    if site_id is None:
        raise _link_not_found()
    with site_scope(site_id):
        async with factory() as session:
            try:
                found = await svc.public_link(session, token, now=now)
            except PurchaseRuleError as exc:
                raise _rule(exc) from exc
            return PublicQuoteLinkOut(
                site_name=found.site_name,
                vendor_name=found.vendor.name,
                number=found.request.number,
                title=found.request.title,
                items=[
                    PublicQuoteItemOut(description=i.description, qty=_qty(i.quantity), unit=i.unit)
                    for i in found.items
                ],
                expires_at=found.link.expires_at,
            )


class PublicQuoteIn(BaseModel):
    lines: list[PurchaseQuotePriceIn] = Field(default_factory=list, max_length=60)
    delivery_days: int | None = Field(default=None, ge=-1, le=10000)
    payment_terms: str | None = Field(default=None, max_length=200)
    valid_until: dt.date | None = None
    note: str | None = Field(default=None, max_length=1000)
    terms_ack: bool = False


class PublicQuoteResult(BaseModel):
    total: Money


@public_router.post(
    "/{token}", status_code=status.HTTP_201_CREATED, summary="Tedarikçi teklifini gönderir"
)
async def public_submit(
    token: str,
    body: PublicQuoteIn,
    request: Request,
    factory: FactoryDep,
    now: NowDep,
    today: TodayDep,
) -> Written[PublicQuoteResult]:
    await _limit(factory, request, now, "post", PUBLIC_POST_LIMIT)
    site_id = await svc.site_of_token(factory, token)
    if site_id is None:
        raise _link_not_found()
    with site_scope(site_id):
        async with factory() as session, session.begin():
            try:
                quote = await svc.submit_public_quote(
                    session,
                    token,
                    _quote_input(body),
                    terms_ack=body.terms_ack,
                    today=today,
                    now=now,
                )
            except PurchaseFieldError as exc:
                raise _form(exc) from exc
            except PurchaseRuleError as exc:
                raise _rule(exc) from exc
            total = quote.total
    return Written(
        data=PublicQuoteResult(total=total),
        message="Teklifiniz iletildi. Teşekkür ederiz; bu bağlantı artık kapandı.",
    )


@public_router.post(
    "/{token}/file",
    status_code=status.HTTP_201_CREATED,
    summary="Gönderilen teklife belge ekle (gönderimden sonra 1 saat, bir kez)",
)
async def public_file(
    token: str,
    request: Request,
    factory: FactoryDep,
    now: NowDep,
    store: StoreDep,
    file: FileField = None,
) -> Written[PurchaseFileOut]:
    await _limit(factory, request, now, "post", PUBLIC_POST_LIMIT)
    site_id = await svc.site_of_token(factory, token)
    if site_id is None:
        raise _link_not_found()
    written: str | None = None
    with site_scope(site_id):
        try:
            async with factory() as session, session.begin():
                quote = await svc.public_file_target(session, token, now=now)
                vendor = await svc.get_vendor(session, quote.vendor_id)
                data = await file.read(MAX_FILE_BYTES + 1) if file is not None else b""
                attached = await orders_svc.attach_file(
                    session,
                    quote,
                    file_name=file.filename if file else None,
                    data=data,
                    uploaded_by=vendor.name if vendor else "Tedarikçi",
                    store=store,
                )
                written = attached.written_path
                out = PurchaseFileOut(id=attached.file.id, file_name=attached.file.file_name)
        except FinanceRuleError as exc:
            if written:
                store.delete(written)
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                FORM_ERROR,
                {"file": exc.message},
            ) from exc
        except PurchaseRuleError as exc:
            raise _rule(exc) from exc
        except BaseException:
            if written:
                store.delete(written)
            raise
    return Written(data=out, message=f"{out.file_name} teklifinize eklendi.")
