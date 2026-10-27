"""Sipariş, mal/hizmet kabul, tedarikçi faturası (üçlü eşleşme) ve ödenecekler — Yazılım
Dokümanı SAT-03..05, servis istekleri 65–67. Açık site kapsamında; transaction'ı çağıran yönetir.

- **Onaysız sipariş verilemez** (talep `approved`); tedarikçi aktif olmalı. Sitede "Sipariş"
  (`purchase_order`) onay kuralı varsa sipariş onaydan sonra yazılır.
- **Kabul:** kalan miktardan fazlası girilemez. Stoklu kalemin stok girişi kabul işleminin
  parçasıdır ve **sistem adına** yazılır (kabul edenin stok yetkisi aranmaz); denetim kaydında
  aktör "Mal kabulü …", gerekçede kabul eden kişi ve sipariş no.
- **Fatura:** teslim alınmış ve henüz faturalanmamış miktarla karşılaştırılır (domain
  `match_invoice`). Fark → `diff_pending`; "Fatura farkı" (`invoice_difference`) onay kuralı varsa
  onay talebi açılır, yoksa faturayı girenden başka `expenses.manage` yetkilisi karar verir.
- **"Ödendi":** aynı işlemde gider (kategori = talebin kategorisi, tedarikçi, belge no = fatura
  no, fatura görüntüsü) + seçilen hesaptan kasa çıkışı — mevcut `expenses.create(paid=True)`
  yolundan (dönem kapalıysa reddedilir; ya ikisi ya hiçbiri).
"""

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.approvals import ActionType, ApprovalStatus, Decision
from site_yonetim.domain.files import check_document
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.purchasing import (
    DECISION_NOTE_MAX,
    INVOICE_NUMBER_MAX,
    INVOICE_NUMBER_MIN,
    LINE_PRICE_MAX,
    NOTE_MAX,
    RATING_MAX,
    RATING_MIN,
    REJECT_NOTE_MIN,
    DecisionKind,
    DecisionStage,
    InvoiceLineInput,
    InvoiceStatus,
    OrderLineView,
    OrderStatus,
    PurchaseFieldError,
    PurchaseRuleError,
    PurchaseStatus,
    VendorStatus,
    clean,
    format_quantity,
    line_total,
    match_invoice,
    order_number,
    parse_quantity,
)
from site_yonetim.models import (
    ApprovalRequest,
    Expense,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseQuote,
    PurchaseQuoteLine,
    PurchaseReceipt,
    PurchaseReceiptLine,
    PurchaseRequest,
    Site,
    StoredFile,
    Vendor,
    VendorInvoice,
    VendorInvoiceLine,
)
from site_yonetim.models.audit import Actor, ActorType, audit_reason, current_actor, set_actor
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import expenses as expenses_svc
from site_yonetim.services import inventory as inventory_svc
from site_yonetim.services import purchasing as purchasing_svc
from site_yonetim.services.expenses import realized
from site_yonetim.services.files import FileStore

Person = approvals_svc.Person
ORDER_NOT_FOUND = "Sipariş bulunamadı."


# --- Sipariş (SAT-03) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderWrite:
    order: PurchaseOrder | None
    approval: ApprovalRequest | None


async def create_order(
    session: AsyncSession,
    site: Site,
    pr: PurchaseRequest,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> OrderWrite:
    """`pr` kilitli gelmeli."""
    if pr.status != PurchaseStatus.APPROVED.value:
        raise PurchaseRuleError(409, "not_approved", "Onaysız sipariş verilemez.")
    if pr.approval_request_id is not None:
        raise PurchaseRuleError(409, "approval_pending", "Sipariş onayı bekleniyor.")
    await _vendor_of(session, pr)
    approval = await purchasing_svc.open_approval(
        session, site, pr, ActionType.PURCHASE_ORDER, requester=actor, today=today, now=now
    )
    if approval is not None:
        return OrderWrite(None, approval)
    return OrderWrite(await write_order(session, pr, by=actor, today=today), None)


async def _vendor_of(session: AsyncSession, pr: PurchaseRequest) -> tuple[PurchaseQuote, Vendor]:
    quote = await session.scalar(
        select(PurchaseQuote).where(PurchaseQuote.id == pr.selected_quote_id)
    )
    vendor = await purchasing_svc.get_vendor(session, quote.vendor_id) if quote else None
    if quote is None or vendor is None or vendor.status != VendorStatus.ACTIVE.value:
        raise PurchaseRuleError(409, "vendor_inactive", "Tedarikçi aktif değil; sipariş verilemez.")
    return quote, vendor


async def write_order(
    session: AsyncSession, pr: PurchaseRequest, *, by: Person, today: date
) -> PurchaseOrder:
    quote, vendor = await _vendor_of(session, pr)
    items = await purchasing_svc.items_of(session, pr.id)
    prices = list(
        await session.scalars(
            select(PurchaseQuoteLine.unit_price)
            .where(PurchaseQuoteLine.quote_id == quote.id)
            .order_by(PurchaseQuoteLine.position)
        )
    )
    sequence = await purchasing_svc.next_sequence(
        session, PurchaseOrder, "purchase_orders", today.year
    )
    order = PurchaseOrder(
        id=uuid.uuid7(),
        number_year=today.year,
        sequence=sequence,
        number=order_number(today.year, sequence),
        request_id=pr.id,
        quote_id=quote.id,
        vendor_id=vendor.id,
        expense_category_id=pr.expense_category_id,
        total=quote.total,
        status=OrderStatus.SENT.value,
        created_by=by.user_id,
        created_by_name=by.name,
    )
    session.add(order)
    await session.flush()
    for position, (item, price) in enumerate(zip(items, prices, strict=True)):
        session.add(
            PurchaseOrderLine(
                order_id=order.id,
                position=position,
                description=item.description,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=price,
                line_total=line_total(item.quantity, price),
                stock_item_id=item.stock_item_id,
                received_quantity=Decimal(0),
            )
        )
    pr.status = PurchaseStatus.ORDERED.value
    await session.flush()
    return order


async def _execute_order(session: AsyncSession, request: ApprovalRequest) -> None:
    site, pr, today, now = await purchasing_svc.approval_context(session, request)
    if (
        pr is None
        or pr.status != PurchaseStatus.APPROVED.value
        or pr.approval_request_id != request.id
    ):
        raise approvals_svc.ExecutionError("Satın alma talebi artık sipariş beklemiyor.")
    names, comment = await purchasing_svc.decider_summary(session, request, Decision.APPROVE)
    await purchasing_svc.apply_decision(
        session,
        site,
        pr,
        stage=DecisionStage.ORDER,
        decision=DecisionKind.APPROVE,
        by=None,
        by_name=names,
        note=comment,
        approval_id=request.id,
        today=today,
        now=now,
    )
    try:
        await write_order(
            session,
            pr,
            by=Person(request.requested_by, request.requested_by_name),
            today=today,
        )
    except PurchaseRuleError as exc:
        raise approvals_svc.ExecutionError(exc.message) from exc


async def _close_order(session: AsyncSession, request: ApprovalRequest) -> None:
    site, pr, today, now = await purchasing_svc.approval_context(session, request)
    if pr is None or pr.approval_request_id != request.id:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        names, comment = await purchasing_svc.decider_summary(session, request, Decision.REJECT)
        kind = DecisionKind.REJECT
    else:
        names, comment, kind = request.requested_by_name, None, DecisionKind.WITHDRAW
    await purchasing_svc.apply_decision(
        session,
        site,
        pr,
        stage=DecisionStage.ORDER,
        decision=kind,
        by=None,
        by_name=names,
        note=comment,
        approval_id=request.id,
        today=today,
        now=now,
    )


approvals_svc.register_executor(ActionType.PURCHASE_ORDER, _execute_order)
approvals_svc.register_closer(ActionType.PURCHASE_ORDER, _close_order)


async def get_order(
    session: AsyncSession, order_id: uuid.UUID, *, lock: bool = False
) -> PurchaseOrder | None:
    query = select(PurchaseOrder).where(PurchaseOrder.id == order_id)
    found: PurchaseOrder | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def order_lines(
    session: AsyncSession, order_id: uuid.UUID, *, lock: bool = False
) -> list[PurchaseOrderLine]:
    query = (
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.order_id == order_id)
        .order_by(PurchaseOrderLine.position)
    )
    return list(await session.scalars(query.with_for_update() if lock else query))


async def invoiced_quantities(
    session: AsyncSession, order_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Kalem başına faturalanmış miktar (itiraz edilen faturalar hariç)."""
    rows = await session.execute(
        select(VendorInvoiceLine.order_line_id, func.sum(VendorInvoiceLine.quantity))
        .join(
            VendorInvoice,
            and_(
                VendorInvoice.id == VendorInvoiceLine.invoice_id,
                VendorInvoice.site_id == VendorInvoiceLine.site_id,
            ),
        )
        .where(
            VendorInvoice.order_id == order_id,
            VendorInvoice.status != InvoiceStatus.DISPUTED.value,
        )
        .group_by(VendorInvoiceLine.order_line_id)
    )
    return {line_id: Decimal(q) for line_id, q in rows.all()}


def orders_query(status: OrderStatus | None) -> Select[PurchaseOrder, str, str]:
    query = (
        select(PurchaseOrder, Vendor.name, PurchaseRequest.number)
        .join(
            Vendor,
            and_(Vendor.id == PurchaseOrder.vendor_id, Vendor.site_id == PurchaseOrder.site_id),
        )
        .join(
            PurchaseRequest,
            and_(
                PurchaseRequest.id == PurchaseOrder.request_id,
                PurchaseRequest.site_id == PurchaseOrder.site_id,
            ),
        )
    )
    if status is not None:
        query = query.where(PurchaseOrder.status == status.value)
    return query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())


# --- Kabul (SAT-04) --------------------------------------------------------------------------


@contextmanager
def system_actor(name: str) -> Iterator[None]:
    """Bu blokta yazılan denetim satırları sistem adına (ör. mal kabulünün stok girişi)."""
    actor = current_actor()
    set_actor(
        Actor(
            None,
            name,
            actor.ip if actor else None,
            actor.user_agent if actor else None,
            ActorType.SYSTEM,
        )
    )
    try:
        yield
    finally:
        set_actor(actor)


async def receive(
    session: AsyncSession,
    order: PurchaseOrder,
    quantities: Sequence[object],
    *,
    note: str | None,
    rating: int | None,
    actor: Person,
) -> tuple[PurchaseReceipt, int]:
    """`order` kilitli gelmeli. Stoklu kalemler aynı işlemde, sistem adına stoğa girer.
    Döner: (kabul, stoğa giren kalem sayısı)."""
    if order.status not in (OrderStatus.SENT.value, OrderStatus.PARTIAL.value):
        raise PurchaseRuleError(409, "invalid_status", "Siparişin tamamı teslim alınmış.")
    lines = await order_lines(session, order.id, lock=True)
    errors: dict[str, str] = {}
    amounts: list[Decimal] = []
    for k, line in enumerate(lines):
        raw = quantities[k] if k < len(quantities) else 0
        try:
            qty = parse_quantity(raw, allow_zero=True)
        except ValueError:
            errors[f"lines.{k}"] = "Geçersiz miktar (en çok 3 ondalık)."
            qty = Decimal(0)
        remaining = line.quantity - line.received_quantity
        if qty > remaining:
            errors[f"lines.{k}"] = (
                f"Kalan {format_quantity(remaining)}; fazla teslim not olarak yazılır."
            )
        amounts.append(qty)
    if not errors and all(a == 0 for a in amounts):
        errors["lines"] = "Teslim alınan miktarı girin."
    if rating is not None and not RATING_MIN <= rating <= RATING_MAX:
        errors["rating"] = f"{RATING_MIN}–{RATING_MAX} puan."
    if errors:
        raise PurchaseFieldError(errors)
    receipt = PurchaseReceipt(
        id=uuid.uuid7(),
        order_id=order.id,
        note=clean(note, NOTE_MAX) or None,
        rating=rating,
        by_user_id=actor.user_id,
        by_name=actor.name,
    )
    session.add(receipt)
    await session.flush()
    stocked: list[tuple[uuid.UUID, Decimal]] = []
    for line, qty in zip(lines, amounts, strict=True):
        if qty <= 0:
            continue
        session.add(PurchaseReceiptLine(receipt_id=receipt.id, order_line_id=line.id, quantity=qty))
        line.received_quantity += qty
        if line.stock_item_id is not None:
            stocked.append((line.stock_item_id, qty))
    order.status = (
        OrderStatus.RECEIVED.value
        if all(line.received_quantity >= line.quantity for line in lines)
        else OrderStatus.PARTIAL.value
    )
    await session.flush()  # kabul, kabul edenin adına yazıldı; stok girişi sistem adına
    label = f"Mal kabulü · {order.number}"
    with system_actor(label), audit_reason(f"{order.number} kabulü — kabul eden: {actor.name}"):
        for item_id, qty in stocked:
            item = await inventory_svc.get_item(session, item_id, lock=True)
            if item is None:  # pragma: no cover — FK
                continue
            await inventory_svc.move(
                session,
                item,
                direction="in",
                quantity=f"{qty:f}",
                note=f"{order.number} kabulü",
                moved_by=label,
            )
    return receipt, len(stocked)


async def receipts_of(session: AsyncSession, order_id: uuid.UUID) -> list[PurchaseReceipt]:
    return list(
        await session.scalars(
            select(PurchaseReceipt)
            .where(PurchaseReceipt.order_id == order_id)
            .order_by(PurchaseReceipt.created_at, PurchaseReceipt.id)
        )
    )


async def receipt_quantities(
    session: AsyncSession, receipt_ids: Sequence[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    if not receipt_ids:
        return {}
    rows = await session.scalars(
        select(PurchaseReceiptLine).where(PurchaseReceiptLine.receipt_id.in_(receipt_ids))
    )
    return {(r.receipt_id, r.order_line_id): r.quantity for r in rows}


async def get_receipt(
    session: AsyncSession, receipt_id: uuid.UUID, *, lock: bool = False
) -> PurchaseReceipt | None:
    query = select(PurchaseReceipt).where(PurchaseReceipt.id == receipt_id)
    found: PurchaseReceipt | None = await session.scalar(query.with_for_update() if lock else query)
    return found


# --- Fatura ve üçlü eşleşme (SAT-05) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceInput:
    order_id: uuid.UUID | None
    number: str | None
    invoice_date: date | None
    due_date: date | None
    lines: list[tuple[object, Decimal | None]]  # (miktar, birim fiyat)


async def _close_if_settled(session: AsyncSession, order: PurchaseOrder) -> None:
    """Tamamı teslim alınmış ve teslim alınanın tamamı eşleşmiş faturadaysa sipariş kapanır."""
    if order.status != OrderStatus.RECEIVED.value:
        return
    lines = await order_lines(session, order.id)
    matched = dict(
        (
            await session.execute(
                select(VendorInvoiceLine.order_line_id, func.sum(VendorInvoiceLine.quantity))
                .join(
                    VendorInvoice,
                    and_(
                        VendorInvoice.id == VendorInvoiceLine.invoice_id,
                        VendorInvoice.site_id == VendorInvoiceLine.site_id,
                    ),
                )
                .where(
                    VendorInvoice.order_id == order.id,
                    VendorInvoice.status.in_(
                        [InvoiceStatus.MATCHED.value, InvoiceStatus.PAID.value]
                    ),
                )
                .group_by(VendorInvoiceLine.order_line_id)
            )
        ).all()
    )
    if all(matched.get(line.id, ZERO) >= line.received_quantity for line in lines):
        order.status = OrderStatus.CLOSED.value
        await session.flush()


async def create_invoice(
    session: AsyncSession,
    site: Site,
    data: InvoiceInput,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> VendorInvoice:
    order = await get_order(session, data.order_id, lock=True) if data.order_id else None
    if order is None:
        raise PurchaseFieldError({"order_id": "Sipariş seçin."})
    lines = await order_lines(session, order.id)
    errors: dict[str, str] = {}
    number = clean(data.number, 100).upper().replace("İ", "I")
    if not INVOICE_NUMBER_MIN <= len(number) <= INVOICE_NUMBER_MAX:
        errors["number"] = f"Fatura no yazın ({INVOICE_NUMBER_MIN}–{INVOICE_NUMBER_MAX} karakter)."
    elif await session.scalar(
        select(VendorInvoice.id).where(
            VendorInvoice.vendor_id == order.vendor_id, VendorInvoice.number == number
        )
    ):
        errors["number"] = "Bu tedarikçinin bu numaralı faturası kayıtlı."
    if data.invoice_date is None or data.invoice_date > today:
        errors["date"] = "Fatura tarihi bugünden ileri olamaz."
    if data.due_date is None or (data.invoice_date and data.due_date < data.invoice_date):
        errors["due_date"] = "Vade fatura tarihinden önce olamaz."
    inputs: list[InvoiceLineInput] = []
    if len(data.lines) != len(lines):
        errors["lines"] = "Siparişin her kalemi için miktar ve birim fiyat girin."
    else:
        for k, (raw_qty, price) in enumerate(data.lines):
            try:
                qty = parse_quantity(raw_qty, allow_zero=True)
            except ValueError:
                errors[f"lines.{k}"] = "Geçersiz miktar (en çok 3 ondalık)."
                continue
            if price is None or price < 0 or price > LINE_PRICE_MAX:
                errors[f"lines.{k}"] = "Birim fiyat sıfır ya da daha büyük olmalı."
                continue
            inputs.append(InvoiceLineInput(qty, price))
        if not errors.get("lines") and len(inputs) == len(lines):
            if all(i.quantity == 0 for i in inputs):
                errors["lines"] = "Fatura kalemlerini girin."
            elif sum((line_total(i.quantity, i.unit_price) for i in inputs), ZERO) <= 0:
                errors["lines"] = "Fatura toplamı sıfırdan büyük olmalı."
    if errors:
        raise PurchaseFieldError(errors)
    pending = await session.scalar(
        select(VendorInvoice.id).where(
            VendorInvoice.order_id == order.id,
            VendorInvoice.status == InvoiceStatus.DIFF_PENDING.value,
        )
    )
    if pending is not None:
        raise PurchaseRuleError(
            409,
            "invoice_pending",
            "Bu siparişte onay bekleyen bir fatura farkı var; önce o sonuçlanmalı.",
        )
    invoiced = await invoiced_quantities(session, order.id)
    views = [
        OrderLineView(
            line.description,
            line.unit,
            line.unit_price,
            max(line.received_quantity - invoiced.get(line.id, ZERO), ZERO),
        )
        for line in lines
    ]
    if all(v.open_quantity == 0 for v in views):
        raise PurchaseRuleError(
            409,
            "no_receipt",
            "Teslim alınmamış sipariş için fatura eşleştirilemez; önce mal/hizmet kabulünü girin.",
        )
    settings = await purchasing_svc.read_settings(session)
    result = match_invoice(views, inputs, settings.tolerance_percent)
    invoice = VendorInvoice(
        id=uuid.uuid7(),
        order_id=order.id,
        vendor_id=order.vendor_id,
        number=number,
        invoice_date=data.invoice_date,
        due_date=data.due_date,
        total=result.total,
        expected_total=result.expected,
        diffs=result.diffs,
        status=(InvoiceStatus.MATCHED if result.matched else InvoiceStatus.DIFF_PENDING).value,
        created_by=actor.user_id,
        created_by_name=actor.name,
    )
    session.add(invoice)
    await session.flush()
    for line, inp in zip(lines, inputs, strict=True):
        session.add(
            VendorInvoiceLine(
                invoice_id=invoice.id,
                order_line_id=line.id,
                quantity=inp.quantity,
                unit_price=inp.unit_price,
                line_total=line_total(inp.quantity, inp.unit_price),
            )
        )
    await session.flush()
    if result.matched:
        await _close_if_settled(session, order)
        return invoice
    if await approvals_svc.match_rule(
        session, ActionType.INVOICE_DIFFERENCE, amount=invoice.total, source=None, today=today
    ):
        vendor_name = await session.scalar(select(Vendor.name).where(Vendor.id == order.vendor_id))
        approval = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.INVOICE_DIFFERENCE,
                title=f"Fatura farkı — {vendor_name} {number} ({order.number})",
                summary={
                    "order": order.number,
                    "vendor": vendor_name,
                    "invoice": number,
                    "total": f"{invoice.total:f}",
                    "expected_total": f"{invoice.expected_total:f}",
                    "diffs": invoice.diffs,
                },
                payload={"vendor_invoice_id": str(invoice.id)},
                amount=invoice.total,
                subject_type="vendor_invoices",
                subject_id=invoice.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        invoice.approval_request_id = approval.id
        await session.flush()
    return invoice


async def get_invoice(
    session: AsyncSession, invoice_id: uuid.UUID, *, lock: bool = False
) -> VendorInvoice | None:
    query = select(VendorInvoice).where(VendorInvoice.id == invoice_id)
    found: VendorInvoice | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def apply_invoice_decision(
    session: AsyncSession,
    invoice: VendorInvoice,
    *,
    approve: bool,
    by_name: str,
    note: str | None,
    now: datetime,
) -> None:
    invoice.status = (InvoiceStatus.MATCHED if approve else InvoiceStatus.DISPUTED).value
    invoice.decided_by_name = by_name[:300]
    invoice.decision_note = note
    invoice.decided_at = now
    invoice.approval_request_id = None
    await session.flush()
    if approve:
        order = await get_order(session, invoice.order_id, lock=True)
        if order is not None:
            await _close_if_settled(session, order)


async def decide_invoice(
    session: AsyncSession,
    site: Site,
    invoice: VendorInvoice,
    decision: str,
    note: str | None,
    *,
    actor: Person,
    can_manage: bool,
    now: datetime,
) -> str:
    if invoice.status != InvoiceStatus.DIFF_PENDING.value:
        raise PurchaseRuleError(409, "invalid_status", "Onay bekleyen fark yok.")
    if decision not in (DecisionKind.APPROVE.value, DecisionKind.REJECT.value):
        raise PurchaseFieldError({"decision": "Karar seçin."})
    text_ = clean(note, DECISION_NOTE_MAX)
    if len(text_) < REJECT_NOTE_MIN:
        raise PurchaseFieldError({"note": "Gerekçe zorunlu."})
    approve = decision == DecisionKind.APPROVE.value
    if invoice.approval_request_id is not None:
        await purchasing_svc.decide_via_approvals(
            session,
            site,
            invoice.approval_request_id,
            reject=not approve,
            note=text_,
            actor=actor,
            now=now,
            self_message="Faturayı giren kişi farkı onaylayamaz.",
        )
        return "Kararınız onay mekanizmasına işlendi."
    if actor.user_id == invoice.created_by:
        raise PurchaseRuleError(403, "self_approval", "Faturayı giren kişi farkı onaylayamaz.")
    if not can_manage:
        raise PurchaseRuleError(403, "forbidden", "Bu işlem için yetkiniz yok.")
    await apply_invoice_decision(
        session, invoice, approve=approve, by_name=actor.name, note=text_, now=now
    )
    return (
        "Fark onaylandı; fatura ödenecekler listesine girdi."
        if approve
        else "Faturaya itiraz edildi; tedarikçiye bildirim taslağı hazırlanır (K5)."
    )


async def _invoice_of(session: AsyncSession, request: ApprovalRequest) -> VendorInvoice | None:
    invoice_id = (request.payload or {}).get("vendor_invoice_id")
    return await get_invoice(session, uuid.UUID(invoice_id), lock=True) if invoice_id else None


async def _execute_invoice(session: AsyncSession, request: ApprovalRequest) -> None:
    invoice = await _invoice_of(session, request)
    if (
        invoice is None
        or invoice.status != InvoiceStatus.DIFF_PENDING.value
        or invoice.approval_request_id != request.id
    ):
        raise approvals_svc.ExecutionError("Fatura artık fark onayı beklemiyor.")
    names, comment = await purchasing_svc.decider_summary(session, request, Decision.APPROVE)
    await apply_invoice_decision(
        session,
        invoice,
        approve=True,
        by_name=names,
        note=comment,
        now=request.decided_at or invoice.created_at,
    )


async def _close_invoice(session: AsyncSession, request: ApprovalRequest) -> None:
    """Ret → itiraz edildi. Geri çekme de itiraz sayılır: fatura ödenecekler listesine giremez,
    düzeltilmiş fatura yeniden girilir."""
    invoice = await _invoice_of(session, request)
    if invoice is None or invoice.approval_request_id != request.id:
        return
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        names, comment = await purchasing_svc.decider_summary(session, request, Decision.REJECT)
    else:
        names, comment = request.requested_by_name, "Fark onayı geri çekildi."
    await apply_invoice_decision(
        session,
        invoice,
        approve=False,
        by_name=names,
        note=comment,
        now=request.decided_at or invoice.created_at,
    )


approvals_svc.register_executor(ActionType.INVOICE_DIFFERENCE, _execute_invoice)
approvals_svc.register_closer(ActionType.INVOICE_DIFFERENCE, _close_invoice)


@dataclass(frozen=True, slots=True)
class InvoiceView:
    invoice: VendorInvoice
    order_number: str
    vendor_name: str
    lines: list[VendorInvoiceLine]
    file_name: str | None


def invoices_query(status: InvoiceStatus | None) -> Select[VendorInvoice, str, str]:
    query = (
        select(VendorInvoice, PurchaseOrder.number, Vendor.name)
        .join(
            PurchaseOrder,
            and_(
                PurchaseOrder.id == VendorInvoice.order_id,
                PurchaseOrder.site_id == VendorInvoice.site_id,
            ),
        )
        .join(
            Vendor,
            and_(Vendor.id == VendorInvoice.vendor_id, Vendor.site_id == VendorInvoice.site_id),
        )
    )
    if status is not None:
        query = query.where(VendorInvoice.status == status.value)
    return query.order_by(VendorInvoice.created_at.desc(), VendorInvoice.id.desc())


async def invoice_views(
    session: AsyncSession, rows: Sequence[tuple[VendorInvoice, str, str]]
) -> list[InvoiceView]:
    ids = [r[0].id for r in rows]
    lines: dict[uuid.UUID, list[VendorInvoiceLine]] = {i: [] for i in ids}
    if ids:
        for line in await session.scalars(
            select(VendorInvoiceLine)
            .join(
                PurchaseOrderLine,
                and_(
                    PurchaseOrderLine.id == VendorInvoiceLine.order_line_id,
                    PurchaseOrderLine.site_id == VendorInvoiceLine.site_id,
                ),
            )
            .where(VendorInvoiceLine.invoice_id.in_(ids))
            .order_by(VendorInvoiceLine.invoice_id, PurchaseOrderLine.position)
        ):
            lines[line.invoice_id].append(line)
    names = await purchasing_svc.file_names(session, [r[0].stored_file_id for r in rows])
    return [
        InvoiceView(
            inv,
            order_no,
            vendor,
            lines[inv.id],
            names.get(inv.stored_file_id) if inv.stored_file_id else None,
        )
        for inv, order_no, vendor in rows
    ]


# --- Ödenecekler ve "ödendi" (servis isteği 67) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Payables:
    invoices: list[InvoiceView]
    direct_count: int
    direct_total: Decimal
    total: Decimal


async def payables(session: AsyncSession) -> Payables:
    """Yalnız eşleşmiş (ödenmemiş) faturalar, vadeye göre. Siparişsiz gider: ödenmemiş
    gerçekleşen giderler (satın alma faturası "ödendi" ile yazıldığından burada yoktur)."""
    rows = list(
        (
            await session.execute(
                invoices_query(InvoiceStatus.MATCHED)
                .order_by(None)
                .order_by(VendorInvoice.due_date, VendorInvoice.id)
            )
        ).all()
    )
    views = await invoice_views(session, rows)
    direct = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.paid_on.is_(None), realized()
            )
        )
    ).one()
    total = sum((v.invoice.total for v in views), ZERO)
    return Payables(views, int(direct[0]), Decimal(direct[1]).quantize(Decimal("0.01")), total)


async def mark_paid(
    session: AsyncSession,
    invoice: VendorInvoice,
    *,
    paid_on: date,
    cash_account_id: uuid.UUID,
    today: date,
    actor: Person,
) -> Expense:
    """`invoice` kilitli gelmeli. Gider + kasa çıkışı aynı işlemde (`expenses.create`)."""
    if invoice.status != InvoiceStatus.MATCHED.value:
        raise PurchaseRuleError(
            409,
            "not_matched",
            "Fatura zaten ödendi."
            if invoice.status == InvoiceStatus.PAID.value
            else "Eşleşmeyen fatura ödenemez.",
        )
    if paid_on > today:
        raise PurchaseFieldError({"paid_on": "Ödeme tarihi bugünden ileri olamaz."})
    order = await get_order(session, invoice.order_id)
    vendor_name = await session.scalar(select(Vendor.name).where(Vendor.id == invoice.vendor_id))
    if order is None or vendor_name is None:  # pragma: no cover — FK
        raise PurchaseRuleError(404, "not_found", ORDER_NOT_FOUND)
    created = await expenses_svc.create(
        session,
        expenses_svc.NewExpense(
            expense_category_id=order.expense_category_id,
            description=f"{vendor_name} — fatura {invoice.number}"[:200],
            amount=invoice.total,
            day=invoice.invoice_date,
            vendor=vendor_name,
            document_number=invoice.number,
            note=f"{order.number} siparişi (satın alma)",
            paid=True,
            paid_on=paid_on,
            cash_account_id=cash_account_id,
            stored_file_id=invoice.stored_file_id,
        ),
        today=today,
        created_by=actor.name,
    )
    invoice.status = InvoiceStatus.PAID.value
    invoice.paid_on = paid_on
    invoice.cash_account_id = cash_account_id
    invoice.expense_id = created.expense.id
    await session.flush()
    return created.expense


# --- Belge (teklif PDF'i, kabul fotoğrafı/irsaliye, fatura görüntüsü) -----------------------


@dataclass(frozen=True, slots=True)
class Attached:
    file: StoredFile
    written_path: str  # işlem geri alınırsa çağıran siler


async def attach_file(
    session: AsyncSession,
    target: PurchaseQuote | PurchaseReceipt | VendorInvoice,
    *,
    file_name: str | None,
    data: bytes,
    uploaded_by: str,
    store: FileStore,
) -> Attached:
    """Kayda bir kez belge bağlanır (`stored_files`; içerik imzası, ≤ 10 MB). `target` kilitli."""
    if target.stored_file_id is not None:
        raise PurchaseRuleError(409, "file_exists", "Bu kayda belge zaten eklenmiş.")
    clean_name, kind = check_document(file_name, data)
    scope = current_scope()
    site_id = scope.site_id if scope and scope.site_id else uuid.UUID(int=0)
    file_id = uuid.uuid7()
    written, digest = store.write(site_id, file_id, kind, data)
    stored = StoredFile(
        id=file_id,
        file_name=clean_name,
        content_type=kind.content_type,
        byte_size=len(data),
        storage_path=written,
        sha256=digest,
        uploaded_by_name=uploaded_by,
    )
    session.add(stored)
    await session.flush()
    target.stored_file_id = file_id
    await session.flush()
    return Attached(stored, written)
