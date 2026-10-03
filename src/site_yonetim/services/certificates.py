"""Borçsuzluk belgesi — frontend servis isteği 01. Açık site kapsamında; transaction'ı çağıran
yönetir.

- Bakiye belge anında **defterden** hesaplanır (özet tablodan değil) ve belgeye yazılır.
  Hesap önce kilitlenir: aynı anda yazılan bir tahakkuk/tahsilat ya önce biter (belge onu
  görür) ya da belgeyi bekler.
- Borçlu hesaba (bakiye > 0,005) belge verilmez; sıfır ya da alacaklı bakiyeye verilir.
- Numara site ve yıl bazında boşluksuz artar: işlem kilidi (advisory lock) altında `max + 1`;
  `(site_id, year, sequence)` benzersizliği ikinci güvence. Belge silinmediği için boşluk oluşmaz.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import EPSILON
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import ClearanceCertificate, LedgerEntry
from site_yonetim.services import ledger
from site_yonetim.services.accounts import AccountRow

VALID_DAYS = 30  # öneri (servis isteği 01): belge tarihinden 30 gün geçerli
_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def certificate_number(year: int, sequence: int) -> str:
    """certificate_number(2026, 1) → "BB-2026-00001" """
    return f"BB-{year}-{sequence:05d}"


async def _ledger_balance(session: AsyncSession, account_id: uuid.UUID) -> Decimal:
    value = await session.scalar(
        select(func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return (value if value is not None else Decimal(0)).quantize(Decimal("0.01"))


async def _next_sequence(session: AsyncSession, year: int) -> int:
    scope = current_scope()
    site = scope.site_id if scope else ""
    await session.execute(_NUMBER_LOCK, {"key": f"clearance_certificates:{site}:{year}"})
    current = await session.scalar(
        select(func.max(ClearanceCertificate.sequence)).where(ClearanceCertificate.year == year)
    )
    return (current or 0) + 1


async def issue(
    session: AsyncSession,
    row: AccountRow,
    *,
    today: date,
    issued_by_user_id: uuid.UUID,
    issued_by_name: str,
) -> ClearanceCertificate:
    account = row.account
    if account.is_closed:
        raise FinanceRuleError("account_closed", "Kapalı hesaba belge düzenlenemez.", conflict=True)
    await ledger.lock_accounts(session, [account.id])
    balance = await _ledger_balance(session, account.id)
    if balance > EPSILON:
        raise FinanceRuleError(
            "has_debt",
            f"Bu hesabın {format_money_tr(balance)} borcu var; borçsuzluk belgesi verilemez.",
            conflict=True,
        )
    sequence = await _next_sequence(session, today.year)
    certificate = ClearanceCertificate(
        id=uuid.uuid7(),
        ledger_account_id=account.id,
        year=today.year,
        sequence=sequence,
        number=certificate_number(today.year, sequence),
        reference_code=account.reference_code,
        account_kind=account.kind,
        unit_name=row.unit_name,
        person_name=row.person_name,
        balance=balance,
        as_of=today,
        valid_until=today + timedelta(days=VALID_DAYS),
        issued_by_user_id=issued_by_user_id,
        issued_by_name=issued_by_name,
    )
    session.add(certificate)
    await session.flush()
    return certificate


async def get(session: AsyncSession, certificate_id: uuid.UUID) -> ClearanceCertificate | None:
    found: ClearanceCertificate | None = await session.scalar(
        select(ClearanceCertificate).where(ClearanceCertificate.id == certificate_id)
    )
    return found
