"""Muhasebe aktarımı (Mod P) — Yazılım Dokümanı FIN-14, §8.4; servis isteği 32.

Okuma `finance.read`; eşleme ve dosya üretimi `finance.charge.post`. Genel fiş dosyası (CSV,
`;`, UTF-8 BOM; istenirse aynı fişler Excel). **Kivi biçimi K7 (Kivi görüşmesi) kararından sonra**
`AccountingExportAdapter` arayüzüyle eklenir. Varsayılan hesap kodları yalnız öneridir.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, TodayDep
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import ChargePost, FinanceRead
from site_yonetim.api.v1.finance_ops_common import actor, form_error, period_param
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.accounting import ADAPTERS, Voucher
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.fiscal import period_key
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import AccountingExport
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import accounting_export as svc

router = APIRouter(prefix="/sites/{slug}/accounting", tags=["muhasebe aktarımı"])
Paging = Annotated[PageParams, Depends()]


class AccountMappingOut(BaseModel):
    key: str = Field(
        description="charge · ad_hoc_charge · payment_cash · payment_bank_transfer · "
        "payment_credit_card · payment_other · expense · refund"
    )
    label: str
    debit_account: str
    credit_account: str


class AccountMappingsOut(BaseModel):
    items: list[AccountMappingOut]


async def _mappings(ctx: FinanceRead | ChargePost) -> AccountMappingsOut:
    return AccountMappingsOut(
        items=[
            AccountMappingOut(
                key=m.key,
                label=m.label,
                debit_account=m.debit_account,
                credit_account=m.credit_account,
            )
            for m in await svc.mappings(ctx.session)
        ]
    )


@router.get("/mappings", summary="Hesap kodu eşlemesi")
async def get_mappings(ctx: FinanceRead) -> AccountMappingsOut:
    return await _mappings(ctx)


class AccountMappingIn(BaseModel):
    key: str
    debit_account: str = Field(default="", max_length=20)
    credit_account: str = Field(default="", max_length=20)


class AccountMappingsIn(BaseModel):
    items: list[AccountMappingIn] = Field(max_length=50)


@router.put("/mappings", summary="Hesap kodu eşlemesini kaydet (yeni sürüm)")
async def save_mappings(
    body: AccountMappingsIn, ctx: ChargePost, current: CurrentUserDep
) -> Written[AccountMappingsOut]:
    """422 `fields.{key}.debit_account`: hesap kodu rakam ve nokta."""
    try:
        await svc.save_mappings(
            ctx.session,
            [(i.key, i.debit_account.strip(), i.credit_account.strip()) for i in body.items],
            actor=actor(current),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _mappings(ctx),
        message="Hesap kodu eşlemesi kaydedildi; yalnız bundan sonra üretilen dosyaları etkiler.",
    )


class AccountingVoucherOut(BaseModel):
    key: str
    source: str
    source_id: str
    date: dt.date
    description: str
    debit_account: str
    credit_account: str
    amount: Money


def _voucher(v: Voucher) -> AccountingVoucherOut:
    return AccountingVoucherOut(
        key=v.key,
        source=v.source,
        source_id=v.source_id,
        date=v.date,
        description=v.description,
        debit_account=v.debit_account,
        credit_account=v.credit_account,
        amount=v.amount,
    )


class AccountingPreviewOut(BaseModel):
    period: str
    vouchers: list[AccountingVoucherOut] = Field(description="aktarılmamış fişler (dönem içi)")
    already_exported: int
    total: Money
    missing_mappings: list[str] = Field(description="eşlemesi eksik işlem tipleri (etiket)")


@router.get("/exports/preview", summary="Aktarılmamış fiş önizlemesi")
async def preview(
    ctx: FinanceRead, today: TodayDep, period: str | None = None
) -> AccountingPreviewOut:
    ym = period_param(period) if period else YearMonth.of(today)
    pv = await svc.preview(ctx.session, ym)
    return AccountingPreviewOut(
        period=period_key(ym),
        vouchers=[_voucher(v) for v in pv.vouchers],
        already_exported=pv.already_exported,
        total=pv.total,
        missing_mappings=pv.missing_mappings,
    )


class AccountingExportOut(BaseModel):
    id: uuid.UUID
    period: str
    voucher_count: int
    total: Money
    file_name: str
    created_by: str
    created_at: dt.datetime


def _batch(b: AccountingExport) -> AccountingExportOut:
    return AccountingExportOut(
        id=b.id,
        period=period_key(YearMonth(b.year, b.month)),
        voucher_count=b.voucher_count,
        total=b.total,
        file_name=b.file_name,
        created_by=b.created_by,
        created_at=b.created_at,
    )


class AccountingExportIn(BaseModel):
    period: str = Field(description="YYYY-MM")


@router.post("/exports", status_code=HTTPStatus.CREATED, summary="Fiş dosyasını üret ve kilitle")
async def create_export(
    body: AccountingExportIn, ctx: ChargePost, current: CurrentUserDep, now: NowDep
) -> Written[AccountingExportOut]:
    """409 `mapping_missing`, `nothing_to_export`. Aynı fiş bir daha aktarılmaz."""
    ym = period_param(body.period)
    try:
        batch = await svc.export(ctx.session, ym, slug=ctx.site.slug, actor=actor(current), now=now)
    except svc.ExportError as exc:
        raise ApiError(HTTPStatus.CONFLICT, exc.code, exc.message) from exc
    record(
        ctx.session,
        AuditAction.EXPORT,
        "accounting_exports",
        batch.id,
        {
            "period": period_key(ym),
            "voucher_count": batch.voucher_count,
            "total": f"{batch.total:.2f}",
            "file_name": batch.file_name,
        },
    )
    await ctx.session.commit()
    return Written(
        data=_batch(batch),
        message=f"{batch.voucher_count} fiş dosyaya yazıldı ve kilitlendi; aynı fişler bir daha "
        "aktarılmaz. Kivi'ye özel biçim K7 kararından sonra eklenecek.",
    )


@router.get("/exports", summary="Aktarım geçmişi (sayfalı)")
async def list_exports(ctx: FinanceRead, paging: Paging) -> Page[AccountingExportOut]:
    total = await ctx.session.scalar(select(func.count()).select_from(AccountingExport)) or 0
    rows = await ctx.session.scalars(
        select(AccountingExport)
        .order_by(AccountingExport.created_at.desc())
        .offset(paging.offset)
        .limit(paging.page_size)
    )
    return Page(
        items=[_batch(b) for b in rows], page=paging.page, page_size=paging.page_size, total=total
    )


@router.get(
    "/exports/{export_id}/file",
    summary="Dosyayı yeniden indir",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, ADAPTERS["xlsx"].media_type: {}}}},
)
async def export_file(
    export_id: uuid.UUID,
    ctx: FinanceRead,
    file_format: Annotated[Literal["csv", "xlsx"], Query(alias="format")] = "csv",
) -> Response:
    batch = await ctx.session.get(AccountingExport, export_id)
    if batch is None:
        raise NotFoundError("Aktarım bulunamadı.")
    if file_format == "csv":
        content, name, media = batch.content, batch.file_name, ADAPTERS["csv"].media_type
    else:
        adapter = ADAPTERS["xlsx"]
        content = adapter.render(await svc.items_of(ctx.session, batch.id))
        name, media = batch.file_name.rsplit(".", 1)[0] + ".xlsx", adapter.media_type
    record(ctx.session, AuditAction.DOWNLOAD, "accounting_exports", batch.id, {"file_name": name})
    await ctx.session.commit()
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
