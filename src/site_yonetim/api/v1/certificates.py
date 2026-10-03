"""Borçsuzluk belgesi — frontend servis isteği 01 (backend issue #24).

- Düzenleme `finance.payment.record` (Yönetici, Muhasebe); okuma `finance.read` (Denetçi okur,
  düzenleyemez). Sakin kendi belgesini görmez (şimdilik yalnız personel).
- `Idempotency-Key` kabul eder: çift tıklamada ikinci numara çıkmaz.
- Kişi adı `people.read` izni olmayana `null` döner (Denetçi kişisel veri görmez, docs/05).
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import (
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.structure import AccountKind
from site_yonetim.models import ClearanceCertificate
from site_yonetim.services import accounts as account_svc
from site_yonetim.services import certificates as svc

router = APIRouter(prefix="/sites/{slug}", tags=["tahsilat ve cari"])
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]


class CertificateSite(BaseModel):
    name: str
    slug: str


class CertificateAccount(BaseModel):
    id: uuid.UUID
    reference_code: str
    kind: AccountKind
    unit_name: str
    person_name: str | None = Field(description="people.read izni yoksa null")


class CertificateOut(BaseModel):
    id: uuid.UUID
    number: str = Field(description="`BB-2026-00001` — site ve yıl bazında boşluksuz")
    site: CertificateSite
    account: CertificateAccount = Field(description="belge anındaki hesap bilgisi")
    balance: Money = Field(description="belge anındaki bakiye (0 ya da eksi = alacaklı)")
    as_of: dt.date
    issued_at: dt.datetime
    issued_by: str
    valid_until: dt.date

    @classmethod
    def of(cls, ctx: SiteContext, c: ClearanceCertificate) -> CertificateOut:
        return cls(
            id=c.id,
            number=c.number,
            site=CertificateSite(name=ctx.site.name, slug=ctx.site.slug),
            account=CertificateAccount(
                id=c.ledger_account_id,
                reference_code=c.reference_code,
                kind=AccountKind(c.account_kind),
                unit_name=c.unit_name,
                person_name=c.person_name if ctx.access.can(Permission.PEOPLE_READ) else None,
            ),
            balance=c.balance,
            as_of=c.as_of,
            issued_at=c.created_at,
            issued_by=c.issued_by_name,
            valid_until=c.valid_until,
        )


@router.post(
    "/accounts/{account_id}/clearance-certificates",
    status_code=status.HTTP_201_CREATED,
    summary="Borçsuzluk belgesi düzenle",
    response_model=Written[CertificateOut],
)
async def issue_certificate(
    account_id: uuid.UUID,
    ctx: PaymentRecord,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[CertificateOut] | JSONResponse:
    """Borçlu hesaba 409 `has_debt`, kapalı hesaba 409 `account_closed`."""
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    row = await account_svc.account_row(ctx.session, account_id)
    if row is None:  # başka sitenin hesabı da burada "yok"tur
        raise NotFoundError("Hesap bulunamadı.")
    try:
        certificate = await svc.issue(
            ctx.session,
            row,
            today=today,
            issued_by_user_id=current.user.id,
            issued_by_name=current.user.full_name,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.refresh(certificate, ["created_at"])
    written = Written(
        data=CertificateOut.of(ctx, certificate),
        message=f"{certificate.number} numaralı borçsuzluk belgesi düzenlendi.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, written)
    return written


@router.get("/clearance-certificates/{certificate_id}", summary="Borçsuzluk belgesi (yazdırma)")
async def get_certificate(certificate_id: uuid.UUID, ctx: FinanceRead) -> CertificateOut:
    certificate = await svc.get(ctx.session, certificate_id)
    if certificate is None:
        raise NotFoundError("Belge bulunamadı.")
    return CertificateOut.of(ctx, certificate)
