"""Satın alma: tedarikçi, ayarlar, bütçe, talep, teklif (tedarikçi linki dahil) ve onay —
Yazılım Dokümanı SAT-01, SAT-02, SAT-06 (kart); servis istekleri 62–64. Sipariş, kabul, fatura
ve ödenecekler `services/purchase_orders.py`. Açık site kapsamında; transaction'ı çağıran yönetir.

Onay (Furkan kararı: yeni modül ilk günden onay mekanizmasıyla, `services/approvals.py`):
- Eksik teklifle seçim → "Teklif istisnası" (`quote_exception`); yeterli teklifle seçim ya da
  istisna onayı → "Satın alma talebi" (`purchase_request`; tutar = seçilen teklif, kaynak
  `over_budget` bütçe aşımında). Sitede o işlem için onay kuralı varsa onay talebi açılır, karar
  Onaylar ekranından (ya da talep ekranından aynı mekanizmayla) verilir; yoksa talep ekranında
  `expenses.manage` yetkilisi karar verir.
- Talep eden kendi talebini onaylayamaz (onay talebinin "talep eden"i satın alma talebini açan
  kişidir; onaycı listesinden çıkar).

Tedarikçi linki: belirteç 256 bit (`secrets.token_urlsafe(32)`), yalnız SHA-256 özeti saklanır,
`link_days` gün geçerli, tek kullanımlık, tek talebe bağlı; tedarikçi yalnız kalem açıklaması,
miktar ve birimi görür.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, extract, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope
from site_yonetim.domain.approvals import ActionType, ApprovalStatus, Decision
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.purchasing import (
    COMMITTED_PURCHASE,
    DECISION_NOTE_MAX,
    DEFAULT_SETTINGS,
    DELIVERY_DAYS_MAX,
    EXCEPTION_REASON_MIN,
    ITEM_MAX,
    ITEMS_MAX,
    LINE_PRICE_MAX,
    NOTE_MAX,
    OPEN_PURCHASE,
    PAYMENT_TERMS_MAX,
    PAYMENT_TERMS_MIN,
    REASON_MAX,
    REASON_MIN,
    REJECT_NOTE_MIN,
    TITLE_MAX,
    TITLE_MIN,
    UNIT_MAX,
    Budget,
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
    Threshold,
    VendorInput,
    VendorStatus,
    check_vendor,
    clean,
    line_total,
    parse_quantity,
    request_number,
    required_quotes,
)
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import (
    ApprovalDecision,
    ApprovalRequest,
    BudgetItem,
    Expense,
    ExpenseCategory,
    PurchaseDecision,
    PurchaseOrder,
    PurchaseQuote,
    PurchaseQuoteLine,
    PurchaseQuoteLink,
    PurchaseReceipt,
    PurchaseRequest,
    PurchaseRequestItem,
    PurchaseSettings,
    Site,
    StockItem,
    StoredFile,
    Vendor,
    VendorInvoice,
)
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import budget as budget_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.expenses import realized

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
TOKEN_BYTES = 32  # 256 bit
TOKEN_MIN, TOKEN_MAX = 32, 64
PUBLIC_FILE_WINDOW = timedelta(hours=1)  # teklif gönderildikten sonra belge ekleme süresi
LINK_CLOSED = "Bu bağlantının süresi doldu veya kullanıldı."
NOT_FOUND = "Satın alma talebi bulunamadı."

Person = approvals_svc.Person


def _closed(code: str, message: str) -> PurchaseRuleError:
    return PurchaseRuleError(409, code, message)


def today_of(now: datetime) -> date:
    return now.astimezone(BUSINESS_TZ).date()


# --- Ayarlar (servis isteği 63) --------------------------------------------------------------


def _settings_of(row: PurchaseSettings | None) -> Settings:
    if row is None:
        return DEFAULT_SETTINGS
    return Settings(
        thresholds=tuple(Threshold(Decimal(t["max"]), int(t["quotes"])) for t in row.thresholds),
        above_quotes=row.above_quotes,
        tolerance_percent=row.tolerance_percent,
        link_days=row.link_days,
    )


async def read_settings(session: AsyncSession) -> Settings:
    return _settings_of(await session.scalar(select(PurchaseSettings)))


async def save_settings(session: AsyncSession, settings: Settings) -> Settings:
    row: PurchaseSettings | None = await session.scalar(select(PurchaseSettings).with_for_update())
    if row is None:
        row = PurchaseSettings()
        session.add(row)
    row.thresholds = [{"max": f"{t.max:f}", "quotes": t.quotes} for t in settings.thresholds]
    row.above_quotes = settings.above_quotes
    row.tolerance_percent = settings.tolerance_percent
    row.link_days = settings.link_days
    await session.flush()
    return settings


# --- Tedarikçi (servis isteği 62) ------------------------------------------------------------


def vendors_query(q: str | None, status: VendorStatus | None) -> Select[Vendor]:
    query = select(Vendor)
    term = ascii_fold(" ".join((q or "").split()))
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(
            or_(
                Vendor.name_key.like(f"%{escaped}%", escape="\\"),
                Vendor.tax_number.like(f"%{escaped}%", escape="\\"),
            )
        )
    if status is not None:
        query = query.where(Vendor.status == status.value)
    return query.order_by(Vendor.name_key, Vendor.id)


async def get_vendor(
    session: AsyncSession, vendor_id: uuid.UUID, *, lock: bool = False
) -> Vendor | None:
    query = select(Vendor).where(Vendor.id == vendor_id)
    found: Vendor | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def save_vendor(session: AsyncSession, data: VendorInput, vendor: Vendor | None) -> Vendor:
    """Yeni ya da düzenleme. Düzenlemede boş IBAN mevcut IBAN'ı korur."""
    checked = check_vendor(data)
    clash = await session.scalar(
        select(Vendor.id).where(
            Vendor.tax_number == checked.tax_number,
            Vendor.id != (vendor.id if vendor else uuid.UUID(int=0)),
        )
    )
    if clash is not None:
        raise PurchaseFieldError({"tax_no": "Bu vergi numarasıyla tedarikçi var."})
    if vendor is None:
        vendor = Vendor(id=uuid.uuid7(), iban=checked.iban)
        session.add(vendor)
    elif checked.iban is not None:
        vendor.iban = checked.iban
    vendor.name = checked.name
    vendor.name_key = ascii_fold(checked.name)
    vendor.tax_number = checked.tax_number
    vendor.phone = checked.phone
    vendor.email = checked.email
    vendor.category = checked.category
    vendor.status = checked.status.value
    vendor.note = checked.note
    await session.flush()
    return vendor


@dataclass(frozen=True, slots=True)
class VendorStats:
    rating_avg: str | None = None  # "4.5"
    rating_count: int = 0
    order_count: int = 0


async def vendor_stats(
    session: AsyncSession, vendor_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, VendorStats]:
    """Puan = mal/hizmet kabulündeki 1–5 puanların ortalaması (1 ondalık); sipariş sayısı."""
    if not vendor_ids:
        return {}
    ratings = await session.execute(
        select(
            PurchaseOrder.vendor_id,
            func.round(func.avg(PurchaseReceipt.rating), 1),
            func.count(PurchaseReceipt.rating),
        )
        .join(
            PurchaseReceipt,
            and_(
                PurchaseReceipt.order_id == PurchaseOrder.id,
                PurchaseReceipt.site_id == PurchaseOrder.site_id,
            ),
        )
        .where(PurchaseOrder.vendor_id.in_(vendor_ids), PurchaseReceipt.rating.is_not(None))
        .group_by(PurchaseOrder.vendor_id)
    )
    orders = await session.execute(
        select(PurchaseOrder.vendor_id, func.count())
        .where(PurchaseOrder.vendor_id.in_(vendor_ids))
        .group_by(PurchaseOrder.vendor_id)
    )
    order_counts = {v: int(c) for v, c in orders.all()}
    out = {v: VendorStats(order_count=order_counts.get(v, 0)) for v in vendor_ids}
    for vendor_id, avg, count in ratings.all():
        out[vendor_id] = VendorStats(
            f"{avg:.1f}" if avg is not None else None, int(count), order_counts.get(vendor_id, 0)
        )
    return out


def _require_active(vendor: Vendor | None, *, field_name: str = "vendor_id") -> Vendor:
    if vendor is None:
        raise PurchaseFieldError({field_name: "Tedarikçi seçin."})
    if vendor.status != VendorStatus.ACTIVE.value:
        raise PurchaseFieldError(
            {
                field_name: "Tedarikçi aktif değil; pasif ya da kara listedeki tedarikçiden teklif "
                "alınmaz."
            }
        )
    return vendor


# --- Bütçe (servis isteği 63) ----------------------------------------------------------------


async def budget(
    session: AsyncSession,
    category_id: uuid.UUID,
    *,
    today: date,
    exclude_request: uuid.UUID | None = None,
) -> Budget:
    """`annual` yürürlükteki işletme projesinde kategoriye bağlı kalemlerin yıllık toplamı;
    `spent` bu yılın gerçekleşen gideri (geri alınan/düzeltme hariç — gider raporuyla aynı
    `realized()` kümesi); `committed` onay bekleyen/onaylı talepler + açık siparişlerin ödenmemiş
    kısmı. Hesap veritabanında yapılır."""
    plan = await budget_svc.current_plan(session)
    annual = ZERO
    if plan is not None:
        annual = (
            await session.scalar(
                select(func.coalesce(func.sum(BudgetItem.annual_amount), 0)).where(
                    BudgetItem.budget_plan_id == plan.id,
                    BudgetItem.expense_category_id == category_id,
                )
            )
        ) or ZERO
    spent = (
        await session.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.expense_category_id == category_id,
                extract("year", Expense.date) == today.year,
                realized(),
            )
        )
    ) or ZERO
    request_amount = func.coalesce(PurchaseQuote.total, PurchaseRequest.estimated_total)
    pending = (
        select(func.coalesce(func.sum(request_amount), 0))
        .select_from(PurchaseRequest)
        .outerjoin(
            PurchaseQuote,
            and_(
                PurchaseQuote.id == PurchaseRequest.selected_quote_id,
                PurchaseQuote.site_id == PurchaseRequest.site_id,
            ),
        )
        .where(
            PurchaseRequest.expense_category_id == category_id,
            PurchaseRequest.status.in_([s.value for s in COMMITTED_PURCHASE]),
        )
    )
    if exclude_request is not None:
        pending = pending.where(PurchaseRequest.id != exclude_request)
    committed_requests = (await session.scalar(pending)) or ZERO
    invoices = (
        select(
            VendorInvoice.order_id.label("order_id"),
            func.sum(
                case(
                    (VendorInvoice.status == InvoiceStatus.PAID.value, VendorInvoice.total), else_=0
                )
            ).label("paid"),
            func.count(
                case(
                    (
                        VendorInvoice.status.in_(
                            [InvoiceStatus.MATCHED.value, InvoiceStatus.DIFF_PENDING.value]
                        ),
                        1,
                    )
                )
            ).label("unpaid"),
        )
        .group_by(VendorInvoice.order_id)
        .subquery()
    )
    open_part = case(
        (
            and_(
                PurchaseOrder.status == OrderStatus.CLOSED.value,
                func.coalesce(invoices.c.unpaid, 0) == 0,
            ),
            0,
        ),
        else_=func.greatest(PurchaseOrder.total - func.coalesce(invoices.c.paid, 0), 0),
    )
    committed_orders = (
        await session.scalar(
            select(func.coalesce(func.sum(open_part), 0))
            .select_from(PurchaseOrder)
            .outerjoin(invoices, invoices.c.order_id == PurchaseOrder.id)
            .where(PurchaseOrder.expense_category_id == category_id)
        )
    ) or ZERO
    cent = Decimal("0.01")
    return Budget(
        has_budget=annual > 0,
        annual=Decimal(annual).quantize(cent),
        spent=Decimal(spent).quantize(cent),
        committed=(Decimal(committed_requests) + Decimal(committed_orders)).quantize(cent),
    )


async def category(session: AsyncSession, category_id: uuid.UUID | None) -> ExpenseCategory | None:
    if category_id is None:
        return None
    found: ExpenseCategory | None = await session.scalar(
        select(ExpenseCategory).where(ExpenseCategory.id == category_id)
    )
    return found


# --- Talep (SAT-01) --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemInput:
    description: str | None
    qty: object
    unit: str | None
    est_unit_price: Decimal | None
    stock_item_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RequestInput:
    title: str | None
    reason: str | None
    category_id: uuid.UUID | None
    items: list[ItemInput]


async def next_sequence(session: AsyncSession, model: Any, key: str, year: int) -> int:
    scope = current_scope()
    await session.execute(_NUMBER_LOCK, {"key": f"{key}:{scope.site_id if scope else ''}:{year}"})
    current = await session.scalar(
        select(func.max(model.sequence)).where(model.number_year == year)
    )
    return int(current or 0) + 1


async def create_request(
    session: AsyncSession, data: RequestInput, *, actor: Person, today: date
) -> PurchaseRequest:
    errors: dict[str, str] = {}
    title = clean(data.title, 200)
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        errors["title"] = f"Konu yazın ({TITLE_MIN}–{TITLE_MAX} karakter)."
    reason = clean(data.reason, 1000)
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        errors["reason"] = f"Gerekçe yazın ({REASON_MIN}–{REASON_MAX} karakter)."
    found_category = await category(session, data.category_id)
    if found_category is None:
        errors["category_id"] = "Gider kategorisi seçin."
    if not data.items:
        errors["items"] = "En az bir kalem ekleyin."
    elif len(data.items) > ITEMS_MAX:
        errors["items"] = f"En çok {ITEMS_MAX} kalem."
    stock_ids = {i.stock_item_id for i in data.items if i.stock_item_id}
    known_stock = (
        set(await session.scalars(select(StockItem.id).where(StockItem.id.in_(stock_ids))))
        if stock_ids
        else set()
    )
    items: list[tuple[str, Decimal, str, Decimal, uuid.UUID | None]] = []
    for k, item in enumerate(data.items[:ITEMS_MAX]):
        description = clean(item.description, 200)
        if not description or len(description) > ITEM_MAX:
            errors[f"items.{k}.description"] = f"Kalem açıklaması yazın (en çok {ITEM_MAX})."
        try:
            qty = parse_quantity(item.qty, allow_zero=False)
        except ValueError:
            errors[f"items.{k}.qty"] = "Miktar sıfırdan büyük olmalı (en çok 3 ondalık)."
            qty = Decimal(0)
        price = item.est_unit_price
        if price is None or price <= 0 or price > LINE_PRICE_MAX:
            errors[f"items.{k}.est_unit_price"] = "Tahmini birim fiyat sıfırdan büyük olmalı."
            price = Decimal(0)
        if item.stock_item_id and item.stock_item_id not in known_stock:
            errors[f"items.{k}.stock_item_id"] = "Stok kartı bulunamadı."
        unit = clean(item.unit, 40)[:UNIT_MAX] or "adet"
        items.append((description, qty, unit, price, item.stock_item_id))
    if errors or found_category is None:
        raise PurchaseFieldError(errors)
    total = sum((line_total(q, p) for _, q, _, p, _ in items), ZERO)
    settings = await read_settings(session)
    snapshot = await budget(session, found_category.id, today=today)
    sequence = await next_sequence(session, PurchaseRequest, "purchase_requests", today.year)
    pr = PurchaseRequest(
        id=uuid.uuid7(),
        number_year=today.year,
        sequence=sequence,
        number=request_number(today.year, sequence),
        title=title,
        reason=reason,
        expense_category_id=found_category.id,
        estimated_total=total,
        budget_annual=snapshot.annual if snapshot.has_budget else None,
        budget_spent=snapshot.spent if snapshot.has_budget else None,
        budget_committed=snapshot.committed if snapshot.has_budget else None,
        over_budget=snapshot.exceeded_by(total),
        required_quotes=required_quotes(settings, total),
        status=PurchaseStatus.QUOTING.value,
        created_by=actor.user_id,
        created_by_name=actor.name,
    )
    session.add(pr)
    await session.flush()
    for position, (description, qty, unit, price, stock_id) in enumerate(items):
        session.add(
            PurchaseRequestItem(
                request_id=pr.id,
                position=position,
                description=description,
                quantity=qty,
                unit=unit,
                est_unit_price=price,
                stock_item_id=stock_id,
            )
        )
    await session.flush()
    return pr


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, lock: bool = False
) -> PurchaseRequest | None:
    query = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
    found: PurchaseRequest | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def items_of(session: AsyncSession, request_id: uuid.UUID) -> list[PurchaseRequestItem]:
    rows = await session.scalars(
        select(PurchaseRequestItem)
        .where(PurchaseRequestItem.request_id == request_id)
        .order_by(PurchaseRequestItem.position)
    )
    return list(rows)


@dataclass(frozen=True, slots=True)
class QuoteView:
    quote: PurchaseQuote
    vendor_name: str
    unit_prices: list[Decimal]
    stats: VendorStats
    file_name: str | None


@dataclass(frozen=True, slots=True)
class LinkView:
    link: PurchaseQuoteLink
    vendor_name: str
    state: LinkState


@dataclass(frozen=True, slots=True)
class RequestView:
    request: PurchaseRequest
    category_name: str
    items: list[PurchaseRequestItem]
    stock_names: dict[uuid.UUID, str]
    quotes: list[QuoteView]
    links: list[LinkView]
    decisions: list[PurchaseDecision]
    order_id: uuid.UUID | None
    order_number: str | None
    approval: ApprovalRequest | None


def link_state(link: PurchaseQuoteLink, request_status: str, now: datetime) -> LinkState:
    if link.used_at is not None:
        return LinkState.USED
    if (
        link.closed_at is not None
        or link.expires_at <= now
        or request_status != PurchaseStatus.QUOTING.value
    ):
        return LinkState.EXPIRED
    return LinkState.OPEN


async def file_names(
    session: AsyncSession, ids: Sequence[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    rows = await session.execute(
        select(StoredFile.id, StoredFile.file_name).where(StoredFile.id.in_(wanted))
    )
    return dict(rows.all())


async def load_request(session: AsyncSession, pr: PurchaseRequest, *, now: datetime) -> RequestView:
    items = await items_of(session, pr.id)
    stock_ids = [i.stock_item_id for i in items if i.stock_item_id]
    stock_names = (
        dict(
            (
                await session.execute(
                    select(StockItem.id, StockItem.name).where(StockItem.id.in_(stock_ids))
                )
            ).all()
        )
        if stock_ids
        else {}
    )
    category_name = (
        await session.scalar(
            select(ExpenseCategory.name).where(ExpenseCategory.id == pr.expense_category_id)
        )
    ) or "—"
    quote_rows = list(
        (
            await session.execute(
                select(PurchaseQuote, Vendor.name)
                .join(
                    Vendor,
                    and_(
                        Vendor.id == PurchaseQuote.vendor_id,
                        Vendor.site_id == PurchaseQuote.site_id,
                    ),
                )
                .where(PurchaseQuote.request_id == pr.id)
                .order_by(PurchaseQuote.created_at, PurchaseQuote.id)
            )
        ).all()
    )
    quote_ids = [q.id for q, _ in quote_rows]
    prices: dict[uuid.UUID, list[Decimal]] = {q: [] for q in quote_ids}
    if quote_ids:
        for line in await session.scalars(
            select(PurchaseQuoteLine)
            .where(PurchaseQuoteLine.quote_id.in_(quote_ids))
            .order_by(PurchaseQuoteLine.quote_id, PurchaseQuoteLine.position)
        ):
            prices[line.quote_id].append(line.unit_price)
    stats = await vendor_stats(session, [q.vendor_id for q, _ in quote_rows])
    names = await file_names(session, [q.stored_file_id for q, _ in quote_rows])
    quotes = [
        QuoteView(
            q,
            vendor_name,
            prices[q.id],
            stats.get(q.vendor_id, VendorStats()),
            names.get(q.stored_file_id) if q.stored_file_id else None,
        )
        for q, vendor_name in quote_rows
    ]
    link_rows = (
        await session.execute(
            select(PurchaseQuoteLink, Vendor.name)
            .join(
                Vendor,
                and_(
                    Vendor.id == PurchaseQuoteLink.vendor_id,
                    Vendor.site_id == PurchaseQuoteLink.site_id,
                ),
            )
            .where(PurchaseQuoteLink.request_id == pr.id)
            .order_by(PurchaseQuoteLink.created_at)
        )
    ).all()
    links = [LinkView(link, name, link_state(link, pr.status, now)) for link, name in link_rows]
    decisions = list(
        await session.scalars(
            select(PurchaseDecision)
            .where(PurchaseDecision.request_id == pr.id)
            .order_by(PurchaseDecision.created_at, PurchaseDecision.id)
        )
    )
    order = (
        await session.execute(
            select(PurchaseOrder.id, PurchaseOrder.number).where(PurchaseOrder.request_id == pr.id)
        )
    ).first()
    approval = (
        await approvals_svc.get_request(session, pr.approval_request_id)
        if pr.approval_request_id
        else None
    )
    return RequestView(
        request=pr,
        category_name=category_name,
        items=items,
        stock_names=stock_names,
        quotes=quotes,
        links=links,
        decisions=decisions,
        order_id=order[0] if order else None,
        order_number=order[1] if order else None,
        approval=approval,
    )


@dataclass(frozen=True, slots=True)
class RequestRow:
    request: PurchaseRequest
    category_name: str
    quote_count: int
    order_id: uuid.UUID | None
    order_number: str | None


def requests_query(status: PurchaseStatus | None) -> Select[Any]:
    quote_count = (
        select(func.count(PurchaseQuote.id))
        .where(PurchaseQuote.request_id == PurchaseRequest.id)
        .correlate(PurchaseRequest)
        .scalar_subquery()
    )
    query = (
        select(
            PurchaseRequest,
            ExpenseCategory.name,
            quote_count,
            PurchaseOrder.id,
            PurchaseOrder.number,
        )
        .join(
            ExpenseCategory,
            and_(
                ExpenseCategory.id == PurchaseRequest.expense_category_id,
                ExpenseCategory.site_id == PurchaseRequest.site_id,
            ),
        )
        .outerjoin(
            PurchaseOrder,
            and_(
                PurchaseOrder.request_id == PurchaseRequest.id,
                PurchaseOrder.site_id == PurchaseRequest.site_id,
            ),
        )
    )
    if status is not None:
        query = query.where(PurchaseRequest.status == status.value)
    return query.order_by(PurchaseRequest.created_at.desc(), PurchaseRequest.id.desc())


# --- Teklif (SAT-02) -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuoteInput:
    unit_prices: list[Decimal | None]
    delivery_days: int | None
    payment_terms: str | None
    valid_until: date | None
    note: str | None


def _check_quote(
    data: QuoteInput, items: list[PurchaseRequestItem], today: date, errors: dict[str, str]
) -> tuple[list[Decimal], int, str, date, str | None]:
    prices: list[Decimal] = []
    if len(data.unit_prices) != len(items):
        errors["lines"] = "Her kalem için birim fiyat girin."
    else:
        for k, price in enumerate(data.unit_prices):
            if price is None or price <= 0 or price > LINE_PRICE_MAX:
                errors[f"lines.{k}"] = "Birim fiyat sıfırdan büyük olmalı."
            else:
                prices.append(price)
    days = data.delivery_days
    if days is None or not 0 <= days <= DELIVERY_DAYS_MAX:
        errors["delivery_days"] = f"Teslim süresi 0–{DELIVERY_DAYS_MAX} gün olmalı."
    terms = clean(data.payment_terms, 200)
    if not PAYMENT_TERMS_MIN <= len(terms) <= PAYMENT_TERMS_MAX:
        errors["payment_terms"] = "Ödeme koşulunu yazın (ör. 30 gün vadeli)."
    if data.valid_until is None or data.valid_until < today:
        errors["valid_until"] = "Teklif geçerliliği bugünden önce olamaz."
    note = clean(data.note, NOTE_MAX) or None
    return prices, days or 0, terms, data.valid_until or today, note


def _require_status(pr: PurchaseRequest, *allowed: PurchaseStatus, message: str) -> None:
    if PurchaseStatus(pr.status) not in allowed:
        raise _closed("invalid_status", message)


async def _write_quote(
    session: AsyncSession,
    pr: PurchaseRequest,
    vendor: Vendor,
    items: list[PurchaseRequestItem],
    prices: list[Decimal],
    *,
    days: int,
    terms: str,
    valid_until: date,
    note: str | None,
    source: QuoteSource,
    by: Person | None,
) -> PurchaseQuote:
    totals = [line_total(i.quantity, p) for i, p in zip(items, prices, strict=True)]
    quote = PurchaseQuote(
        id=uuid.uuid7(),
        request_id=pr.id,
        vendor_id=vendor.id,
        total=sum(totals, ZERO),
        delivery_days=days,
        payment_terms=terms,
        valid_until=valid_until,
        note=note,
        source=source.value,
        created_by=by.user_id if by else None,
        created_by_name=by.name if by else vendor.name,
    )
    session.add(quote)
    await session.flush()
    for position, (price, total) in enumerate(zip(prices, totals, strict=True)):
        session.add(
            PurchaseQuoteLine(
                quote_id=quote.id, position=position, unit_price=price, line_total=total
            )
        )
    await session.flush()
    return quote


async def _quoted(session: AsyncSession, pr: PurchaseRequest, vendor_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(PurchaseQuote.id).where(
                PurchaseQuote.request_id == pr.id, PurchaseQuote.vendor_id == vendor_id
            )
        )
    ) is not None


async def add_quote(
    session: AsyncSession,
    pr: PurchaseRequest,
    vendor_id: uuid.UUID | None,
    data: QuoteInput,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> PurchaseQuote:
    """Personel girişi. `pr` kilitli gelmeli."""
    _require_status(pr, PurchaseStatus.QUOTING, message="Teklif yalnız teklif aşamasında eklenir.")
    errors: dict[str, str] = {}
    vendor = await get_vendor(session, vendor_id) if vendor_id else None
    try:
        _require_active(vendor)
    except PurchaseFieldError as exc:
        errors.update(exc.fields)
    if vendor is not None and "vendor_id" not in errors and await _quoted(session, pr, vendor.id):
        errors["vendor_id"] = "Bu tedarikçinin teklifi var."
    items = await items_of(session, pr.id)
    prices, days, terms, valid_until, note = _check_quote(data, items, today, errors)
    if errors or vendor is None:
        raise PurchaseFieldError(errors)
    quote = await _write_quote(
        session,
        pr,
        vendor,
        items,
        prices,
        days=days,
        terms=terms,
        valid_until=valid_until,
        note=note,
        source=QuoteSource.STAFF,
        by=actor,
    )
    # Aynı tedarikçiye açık link varsa kapanır (teklif artık var).
    for link in await session.scalars(
        select(PurchaseQuoteLink).where(
            PurchaseQuoteLink.request_id == pr.id,
            PurchaseQuoteLink.vendor_id == vendor.id,
            PurchaseQuoteLink.used_at.is_(None),
            PurchaseQuoteLink.closed_at.is_(None),
        )
    ):
        link.closed_at = now
    await session.flush()
    return quote


async def quote_count(session: AsyncSession, request_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PurchaseQuote)
            .where(PurchaseQuote.request_id == request_id)
        )
        or 0
    )


# --- Tedarikçi linki -------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NewLink:
    token: str
    link: PurchaseQuoteLink
    vendor: Vendor
    days: int


async def create_link(
    session: AsyncSession,
    pr: PurchaseRequest,
    vendor_id: uuid.UUID | None,
    *,
    actor: Person,
    now: datetime,
) -> NewLink:
    _require_status(pr, PurchaseStatus.QUOTING, message="Link yalnız teklif aşamasında gönderilir.")
    vendor = _require_active(await get_vendor(session, vendor_id) if vendor_id else None)
    open_link = await session.scalar(
        select(PurchaseQuoteLink.id).where(
            PurchaseQuoteLink.request_id == pr.id,
            PurchaseQuoteLink.vendor_id == vendor.id,
            PurchaseQuoteLink.used_at.is_(None),
            PurchaseQuoteLink.closed_at.is_(None),
            PurchaseQuoteLink.expires_at > now,
        )
    )
    if open_link is not None:
        raise _closed("link_open", "Bu tedarikçiye gönderilmiş açık link var.")
    if await _quoted(session, pr, vendor.id):
        raise _closed("quoted", "Bu tedarikçinin teklifi zaten var.")
    settings = await read_settings(session)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    link = PurchaseQuoteLink(
        id=uuid.uuid7(),
        request_id=pr.id,
        vendor_id=vendor.id,
        token_hash=token_hash(token),
        expires_at=now + timedelta(days=settings.link_days),
        created_by_name=actor.name,
    )
    session.add(link)
    await session.flush()
    return NewLink(token, link, vendor, settings.link_days)


async def site_of_token(factory: async_sessionmaker[AsyncSession], token: str) -> uuid.UUID | None:
    """Herkese açık uç: belirteç özetinden site (bilinçli "tüm siteler" kapsamı, yalnız kimlik)."""
    if not TOKEN_MIN <= len(token) <= TOKEN_MAX:
        return None
    with all_sites_scope():
        async with factory() as session:
            site_id: uuid.UUID | None = await session.scalar(
                select(PurchaseQuoteLink.site_id).where(
                    PurchaseQuoteLink.token_hash == token_hash(token)
                )
            )
    return site_id


@dataclass(frozen=True, slots=True)
class PublicLink:
    link: PurchaseQuoteLink
    request: PurchaseRequest
    vendor: Vendor
    site_name: str
    items: list[PurchaseRequestItem]


async def public_link(
    session: AsyncSession, token: str, *, now: datetime, lock: bool = False
) -> PublicLink:
    """Açık site kapsamında. Bilinmeyen belirteç 404; kullanılmış/süresi dolmuş/kapanmış, talep
    teklif aşamasından çıkmış ya da tedarikçi pasifleşmişse 410."""
    query = select(PurchaseQuoteLink).where(PurchaseQuoteLink.token_hash == token_hash(token))
    link: PurchaseQuoteLink | None = await session.scalar(
        query.with_for_update() if lock else query
    )
    if link is None:
        raise PurchaseRuleError(404, "not_found", "Bağlantı bulunamadı.")
    pr = await get_request(session, link.request_id, lock=lock)
    vendor = await get_vendor(session, link.vendor_id)
    if (
        pr is None
        or vendor is None
        or link_state(link, pr.status, now) is not LinkState.OPEN
        or vendor.status != VendorStatus.ACTIVE.value
    ):
        raise PurchaseRuleError(410, "link_closed", LINK_CLOSED)
    site_name = (await session.scalar(select(Site.name).where(Site.id == pr.site_id))) or ""
    return PublicLink(link, pr, vendor, site_name, await items_of(session, pr.id))


async def submit_public_quote(
    session: AsyncSession,
    token: str,
    data: QuoteInput,
    *,
    terms_ack: bool,
    today: date,
    now: datetime,
) -> PurchaseQuote:
    found = await public_link(session, token, now=now, lock=True)
    errors: dict[str, str] = {}
    prices, days, terms, valid_until, note = _check_quote(data, found.items, today, errors)
    if not terms_ack:
        errors["terms_ack"] = "Teklifin doğruluğunu ve aydınlatma metnini onaylayın."
    if errors:
        raise PurchaseFieldError(errors)
    if await _quoted(session, found.request, found.vendor.id):
        raise PurchaseRuleError(410, "link_closed", LINK_CLOSED)
    quote = await _write_quote(
        session,
        found.request,
        found.vendor,
        found.items,
        prices,
        days=days,
        terms=terms,
        valid_until=valid_until,
        note=note,
        source=QuoteSource.VENDOR_LINK,
        by=None,
    )
    found.link.used_at = now
    found.link.quote_id = quote.id
    await session.flush()
    return quote


async def public_file_target(session: AsyncSession, token: str, *, now: datetime) -> PurchaseQuote:
    """Teklif gönderildikten sonra 1 saat içinde, bir kez belge eklenebilir."""
    link: PurchaseQuoteLink | None = await session.scalar(
        select(PurchaseQuoteLink).where(PurchaseQuoteLink.token_hash == token_hash(token))
    )
    if link is None:
        raise PurchaseRuleError(404, "not_found", "Bağlantı bulunamadı.")
    quote = (
        await session.scalar(
            select(PurchaseQuote).where(PurchaseQuote.id == link.quote_id).with_for_update()
        )
        if link.quote_id
        else None
    )
    if (
        quote is None
        or link.used_at is None
        or now - link.used_at > PUBLIC_FILE_WINDOW
        or quote.stored_file_id is not None
    ):
        raise PurchaseRuleError(
            410, "link_closed", "Belge ekleme süresi doldu ya da belge zaten eklendi."
        )
    return quote


# --- Seçim ve onay ---------------------------------------------------------------------------


async def _rule_exists(
    session: AsyncSession,
    action: ActionType,
    amount: Decimal | None,
    source: str | None,
    today: date,
) -> bool:
    return (
        await approvals_svc.match_rule(session, action, amount=amount, source=source, today=today)
    ) is not None


async def _selected_total(session: AsyncSession, pr: PurchaseRequest) -> Decimal:
    if pr.selected_quote_id is None:
        return pr.estimated_total
    total = await session.scalar(
        select(PurchaseQuote.total).where(PurchaseQuote.id == pr.selected_quote_id)
    )
    return total if total is not None else pr.estimated_total


def _creator(pr: PurchaseRequest) -> Person:
    return Person(pr.created_by or uuid.UUID(int=0), pr.created_by_name)


async def open_approval(
    session: AsyncSession,
    site: Site,
    pr: PurchaseRequest,
    action: ActionType,
    *,
    requester: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    """İşlem için onay kuralı varsa onay talebi açar (yoksa `None` — doğrudan akış)."""
    amount = await _selected_total(session, pr)
    source = "over_budget" if action is ActionType.PURCHASE_REQUEST and pr.over_budget else None
    if not await _rule_exists(session, action, amount, source, today):
        return None
    vendor_name = None
    if pr.selected_quote_id is not None:
        vendor_name = await session.scalar(
            select(Vendor.name)
            .join(
                PurchaseQuote,
                and_(PurchaseQuote.vendor_id == Vendor.id, PurchaseQuote.site_id == Vendor.site_id),
            )
            .where(PurchaseQuote.id == pr.selected_quote_id)
        )
    labels = {
        ActionType.QUOTE_EXCEPTION: "Teklif istisnası",
        ActionType.PURCHASE_REQUEST: "Satın alma",
        ActionType.PURCHASE_ORDER: "Sipariş",
    }
    approval = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=action,
            title=f"{labels[action]} — {pr.number} · {pr.title}",
            summary={
                "number": pr.number,
                "title": pr.title,
                "vendor": vendor_name,
                "amount": f"{amount:f}",
                "estimated_total": f"{pr.estimated_total:f}",
                "required_quotes": pr.required_quotes,
                "quote_count": await quote_count(session, pr.id),
                "over_budget": pr.over_budget,
                "exception_reason": pr.exception_reason,
            },
            payload={"purchase_request_id": str(pr.id)},
            amount=amount,
            source=source,
            subject_type="purchase_requests",
            subject_id=pr.id,
        ),
        requester=requester,
        today=today,
        now=now,
    )
    pr.approval_request_id = approval.id
    await session.flush()
    return approval


async def select_quote(
    session: AsyncSession,
    site: Site,
    pr: PurchaseRequest,
    quote_id: uuid.UUID | None,
    exception_reason: str | None,
    *,
    today: date,
    now: datetime,
) -> PurchaseRequest:
    """`pr` kilitli gelmeli. Eksik teklif → teklif istisnası; yeterli → satın alma onayı."""
    _require_status(pr, PurchaseStatus.QUOTING, message="Teklif yalnız teklif aşamasında seçilir.")
    quote = (
        await session.scalar(
            select(PurchaseQuote).where(
                PurchaseQuote.id == quote_id, PurchaseQuote.request_id == pr.id
            )
        )
        if quote_id
        else None
    )
    if quote is None:
        raise PurchaseFieldError({"quote_id": "Teklif seçin."})
    count = await quote_count(session, pr.id)
    pr.selected_quote_id = quote.id
    if count < pr.required_quotes:
        reason = clean(exception_reason, 1000)
        if len(reason) < EXCEPTION_REASON_MIN:
            raise PurchaseRuleError(
                422,
                "quote_exception",
                f"{pr.required_quotes} teklif gerekiyor, {count} var. Eksik teklif için gerekçe "
                "yazın (teklif istisnası onaya düşer).",
                {
                    "exception_reason": f"Eksik teklif gerekçesi (en az {EXCEPTION_REASON_MIN} "
                    "karakter)."
                },
            )
        pr.exception_reason = reason[:REASON_MAX]
        pr.status = PurchaseStatus.EXCEPTION_PENDING.value
        await session.flush()
        await open_approval(
            session,
            site,
            pr,
            ActionType.QUOTE_EXCEPTION,
            requester=_creator(pr),
            today=today,
            now=now,
        )
        return pr
    pr.exception_reason = None
    pr.status = PurchaseStatus.APPROVAL_PENDING.value
    await session.flush()
    await open_approval(
        session,
        site,
        pr,
        ActionType.PURCHASE_REQUEST,
        requester=_creator(pr),
        today=today,
        now=now,
    )
    return pr


async def _notify_creator(
    session: AsyncSession, pr: PurchaseRequest, kind: str, title: str, body: str | None
) -> None:
    if pr.created_by is None:
        return
    await notify_svc.notify(
        session,
        [pr.created_by],
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=body,
            link_entity="purchase_requests",
            link_id=pr.id,
        ),
        dedupe_key=f"purchase:{pr.id}:{kind}:{len(body or '')}:{pr.status}",
    )


async def apply_decision(
    session: AsyncSession,
    site: Site,
    pr: PurchaseRequest,
    *,
    stage: DecisionStage,
    decision: DecisionKind,
    by: Person | None,
    by_name: str,
    note: str | None,
    approval_id: uuid.UUID | None,
    today: date,
    now: datetime,
) -> None:
    """Kararı talebe uygular (doğrudan ya da onay mekanizmasının yürütücüsünden)."""
    session.add(
        PurchaseDecision(
            request_id=pr.id,
            stage=stage.value,
            decision=decision.value,
            by_user_id=by.user_id if by else None,
            by_name=by_name[:300],
            note=note,
            approval_request_id=approval_id,
        )
    )
    pr.approval_request_id = None
    approve = decision is DecisionKind.APPROVE
    if stage is DecisionStage.EXCEPTION:
        if approve:
            pr.status = PurchaseStatus.APPROVAL_PENDING.value
            await session.flush()
            await open_approval(
                session,
                site,
                pr,
                ActionType.PURCHASE_REQUEST,
                requester=_creator(pr),
                today=today,
                now=now,
            )
            return
        pr.status = PurchaseStatus.QUOTING.value
        pr.selected_quote_id = None
        await session.flush()
        if decision is DecisionKind.REJECT:
            await _notify_creator(
                session,
                pr,
                "purchase.exception_rejected",
                f"Teklif istisnası reddedildi: {pr.number}",
                note,
            )
        return
    if stage is DecisionStage.PURCHASE:
        if approve:
            pr.status = PurchaseStatus.APPROVED.value
            await session.flush()
            await _notify_creator(
                session, pr, "purchase.approved", f"Satın alma onaylandı: {pr.number}", None
            )
            return
        if decision is DecisionKind.REJECT:
            pr.status = PurchaseStatus.REJECTED.value
            pr.reject_reason = note
            await session.flush()
            await _notify_creator(
                session, pr, "purchase.rejected", f"Satın alma talebi reddedildi: {pr.number}", note
            )
            return
        pr.status = PurchaseStatus.QUOTING.value  # geri çekildi: seçim yeniden yapılır
        pr.selected_quote_id = None
        await session.flush()
        return
    await session.flush()  # sipariş aşaması: siparişi `purchase_orders` yazar


async def decide(
    session: AsyncSession,
    site: Site,
    pr: PurchaseRequest,
    decision: str,
    note: str | None,
    *,
    actor: Person,
    can_manage: bool,
    today: date,
    now: datetime,
) -> str:
    """Talep ekranından karar. Onay talebi açıksa karar onay mekanizmasına iletilir (aynı
    kurallar: onaycı listesi, M-of-N, vekâlet). Mesaj döner."""
    _require_status(
        pr,
        PurchaseStatus.EXCEPTION_PENDING,
        PurchaseStatus.APPROVAL_PENDING,
        message="Onay bekleyen bir adım yok.",
    )
    if decision not in (DecisionKind.APPROVE.value, DecisionKind.REJECT.value):
        raise PurchaseFieldError({"decision": "Karar seçin."})
    text_ = clean(note, DECISION_NOTE_MAX) or None
    reject = decision == DecisionKind.REJECT.value
    if reject and len(text_ or "") < REJECT_NOTE_MIN:
        raise PurchaseFieldError({"note": "Ret gerekçesi yazın."})
    stage = (
        DecisionStage.EXCEPTION
        if pr.status == PurchaseStatus.EXCEPTION_PENDING.value
        else DecisionStage.PURCHASE
    )
    if pr.approval_request_id is not None:
        await decide_via_approvals(
            session,
            site,
            pr.approval_request_id,
            reject=reject,
            note=text_,
            actor=actor,
            now=now,
            self_message="Kendi talebinizi onaylayamazsınız.",
        )
        return "Kararınız onay mekanizmasına işlendi."
    if actor.user_id == pr.created_by:
        raise PurchaseRuleError(403, "self_approval", "Kendi talebinizi onaylayamazsınız.")
    if not can_manage:
        raise PurchaseRuleError(403, "forbidden", "Bu işlem için yetkiniz yok.")
    await apply_decision(
        session,
        site,
        pr,
        stage=stage,
        decision=DecisionKind.REJECT if reject else DecisionKind.APPROVE,
        by=actor,
        by_name=actor.name,
        note=text_,
        approval_id=None,
        today=today,
        now=now,
    )
    if stage is DecisionStage.EXCEPTION:
        return (
            "Teklif istisnası reddedildi; teklif toplamaya devam."
            if reject
            else "Teklif istisnası onaylandı; satın alma onayına geçti."
        )
    return (
        "Talep reddedildi; talep edene gerekçe iletildi."
        if reject
        else "Satın alma onaylandı; sipariş oluşturulabilir."
    )


async def decide_via_approvals(
    session: AsyncSession,
    site: Site,
    approval_id: uuid.UUID,
    *,
    reject: bool,
    note: str | None,
    actor: Person,
    now: datetime,
    self_message: str,
) -> None:
    approval = await approvals_svc.get_request(session, approval_id, lock=True)
    if approval is None:  # pragma: no cover — FK
        raise PurchaseRuleError(404, "not_found", "Onay talebi bulunamadı.")
    try:
        await approvals_svc.decide(
            session,
            site,
            approval,
            decider=actor,
            decision=Decision.REJECT if reject else Decision.APPROVE,
            comment=note,
            now=now,
        )
    except approvals_svc.ApprovalError as exc:
        if exc.code == "own_request":
            raise PurchaseRuleError(403, "self_approval", self_message) from exc
        raise PurchaseRuleError(exc.status, exc.code, exc.message) from exc
    except FormFieldError as exc:
        raise PurchaseFieldError({"note": "Gerekçe yazın."}) from exc


async def cancel(
    session: AsyncSession, pr: PurchaseRequest, *, actor: Person, can_manage: bool, now: datetime
) -> PurchaseRequest:
    if actor.user_id != pr.created_by and not can_manage:
        raise PurchaseRuleError(
            403, "forbidden", "Talebi yalnız açan kişi ya da yetkili iptal eder."
        )
    if PurchaseStatus(pr.status) not in OPEN_PURCHASE:
        raise _closed("invalid_status", "Bu talep iptal edilemez.")
    for link in await session.scalars(
        select(PurchaseQuoteLink).where(
            PurchaseQuoteLink.request_id == pr.id,
            PurchaseQuoteLink.used_at.is_(None),
            PurchaseQuoteLink.closed_at.is_(None),
        )
    ):
        link.closed_at = now
    if pr.approval_request_id is not None:
        approval = await approvals_svc.get_request(session, pr.approval_request_id, lock=True)
        if approval is not None and ApprovalStatus(approval.status) in (
            ApprovalStatus.PENDING,
            ApprovalStatus.REVISION_REQUESTED,
        ):
            approval.status = ApprovalStatus.WITHDRAWN.value
            approval.decided_at = now
        pr.approval_request_id = None
    pr.status = PurchaseStatus.CANCELLED.value
    await session.flush()
    return pr


# --- Onay mekanizması yürütücüleri -----------------------------------------------------------


async def approval_context(
    session: AsyncSession, request: ApprovalRequest
) -> tuple[Site, PurchaseRequest | None, date, datetime]:
    scope = current_scope()
    site = await session.get(Site, scope.site_id if scope else None)
    if site is None:  # pragma: no cover — onay site kapsamında yürür
        raise RuntimeError("Site kapsamı yok")
    pr_id = (request.payload or {}).get("purchase_request_id")
    pr = await get_request(session, uuid.UUID(pr_id), lock=True) if pr_id else None
    now = request.decided_at or datetime.now(UTC)
    return site, pr, today_of(now), now


async def decider_summary(
    session: AsyncSession, request: ApprovalRequest, decision: Decision
) -> tuple[str, str | None]:
    """Onay talebindeki karar verenlerin adları ve son gerekçe."""
    rows = (
        await session.execute(
            select(ApprovalDecision.user_name, ApprovalDecision.comment)
            .where(
                ApprovalDecision.request_id == request.id,
                ApprovalDecision.revision == request.revision,
                ApprovalDecision.decision == decision.value,
            )
            .order_by(ApprovalDecision.created_at)
        )
    ).all()
    names = ", ".join(n for n, _ in rows) or request.requested_by_name
    comments = [c for _, c in rows if c]
    return names, (comments[-1] if comments else None)


_STAGES = {
    ActionType.QUOTE_EXCEPTION: (DecisionStage.EXCEPTION, PurchaseStatus.EXCEPTION_PENDING),
    ActionType.PURCHASE_REQUEST: (DecisionStage.PURCHASE, PurchaseStatus.APPROVAL_PENDING),
}


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    site, pr, today, now = await approval_context(session, request)
    stage, expected = _STAGES[ActionType(request.action_type)]
    if pr is None or pr.status != expected.value or pr.approval_request_id != request.id:
        raise approvals_svc.ExecutionError("Satın alma talebi artık bu onayı beklemiyor.")
    names, comment = await decider_summary(session, request, Decision.APPROVE)
    await apply_decision(
        session,
        site,
        pr,
        stage=stage,
        decision=DecisionKind.APPROVE,
        by=None,
        by_name=names,
        note=comment,
        approval_id=request.id,
        today=today,
        now=now,
    )


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    site, pr, today, now = await approval_context(session, request)
    stage, expected = _STAGES[ActionType(request.action_type)]
    if pr is None or pr.status != expected.value or pr.approval_request_id != request.id:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        names, comment = await decider_summary(session, request, Decision.REJECT)
        kind = DecisionKind.REJECT
    else:
        names, comment, kind = request.requested_by_name, None, DecisionKind.WITHDRAW
    await apply_decision(
        session,
        site,
        pr,
        stage=stage,
        decision=kind,
        by=None,
        by_name=names,
        note=comment,
        approval_id=request.id,
        today=today,
        now=now,
    )


for _action in _STAGES:
    approvals_svc.register_executor(_action, _execute)
    approvals_svc.register_closer(_action, _close)
