"""Demo verisi — Finans Mod P işlemleri (servis istekleri 27–33): ekranlar boş görünmesin.

- En eski demo ayı **kapanmış ve kilitli**: kasa günleri sayımla kapandı, muhasebe fişleri
  aktarıldı, kapanışı muhasebe başlattı, yönetici kilitledi (iki kişi kuralı).
- En borçlu iki hesapta yürürlükte ödeme planı; borçlu hesaplarda hatırlatma, arama ve tebliğ
  edilmiş bir ihtarname.
- Dönem dışı ücretler: anında yazılan iki ücret, sonraki tahakkuku bekleyen bir ücret, iptal
  edilmiş bir ücret.
Hepsi gerçek servislerden geçer (defter, kilit ve onay kuralları aynı).
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import PeriodStatus
from site_yonetim.domain.money import EPSILON
from site_yonetim.models import (
    AccountBalance,
    CashAccount,
    CashMovement,
    CollectionAction,
    LedgerAccount,
    PeriodEvent,
    Site,
)
from site_yonetim.services import (
    accounting_export,
    ad_hoc_charges,
    cash_days,
    debt_tracking,
    payment_plans,
    period_lock,
)
from site_yonetim.services.approvals import Person
from site_yonetim.services.charging import BUSINESS_TZ

ACCOUNTANT = "Demo verisi — Muhasebe"
MANAGER = "Demo verisi — Yönetici"


def _actor(name: str) -> Person:
    return Person(None, name)  # type: ignore[arg-type]  # demo: kullanıcıya bağlı değil


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour, 0), BUSINESS_TZ)


async def seed_finance_ops(
    session: AsyncSession, site: Site, first_month: YearMonth, today: date
) -> None:
    await _close_month(session, site, first_month)
    await _plans_and_follow_up(session, site, today)
    await _ad_hoc(session, site, today)


async def _close_month(session: AsyncSession, site: Site, month: YearMonth) -> None:
    """Ayın kasa günlerini sayımla kapatır, fişleri aktarır, dönemi kapanış + kilitle bitirir."""
    end = _at(month.last_day, 18)
    tills = list(await session.scalars(select(CashAccount).where(CashAccount.kind == "cash")))
    for till in tills:
        days = sorted(
            set(
                await session.scalars(
                    select(CashMovement.date).where(
                        CashMovement.cash_account_id == till.id,
                        CashMovement.date >= month.first_day,
                        CashMovement.date <= month.last_day,
                    )
                )
            )
        )
        for day in days:
            system = await cash_days.balance_at(session, till.id, day)
            await cash_days.close_day(
                session,
                site,
                till,
                day=day,
                counted=system,
                reason=None,
                actor=_actor(ACCOUNTANT),
                today=day,
                now=_at(day, 18),
            )
    if (await accounting_export.preview(session, month)).vouchers:
        await accounting_export.export(
            session, month, slug=site.slug, actor=_actor(ACCOUNTANT), now=end
        )
    row = await period_lock.get_or_create(session, month)
    row.status = PeriodStatus.LOCKED.value
    row.closing_started_by = ACCOUNTANT
    row.closing_started_at = end + timedelta(days=2)
    row.locked_by = MANAGER
    row.locked_at = end + timedelta(days=3)
    session.add_all(
        [
            PeriodEvent(period_id=row.id, action="start_close", by_name=ACCOUNTANT),
            PeriodEvent(period_id=row.id, action="lock", by_name=MANAGER),
        ]
    )
    await session.flush()


async def _debtors(session: AsyncSession, limit: int) -> list[LedgerAccount]:
    return list(
        await session.scalars(
            select(LedgerAccount)
            .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
            .where(AccountBalance.balance > EPSILON)
            .order_by(AccountBalance.balance.desc(), LedgerAccount.reference_code)
            .limit(limit)
        )
    )


async def _plans_and_follow_up(session: AsyncSession, site: Site, today: date) -> None:
    debtors = await _debtors(session, 6)
    for account in debtors[:2]:
        balance = await session.scalar(
            select(AccountBalance.balance).where(AccountBalance.account_id == account.id)
        )
        if balance is None or balance < Decimal("100.00"):
            continue
        await payment_plans.create(
            session,
            site,
            account,
            amount=balance,
            installment_count=3,
            first_due_date=today + timedelta(days=10),
            note="Demo: borç yapılandırma",
            actor=_actor(MANAGER),
            today=today,
            now=_at(today, 10),
        )
    for index, account in enumerate(debtors[2:]):
        session.add_all(
            [
                CollectionAction(
                    account_id=account.id,
                    kind="reminder",
                    channel="sms",
                    note="Kademe 2 — SMS hatırlatması",
                    by_name=ACCOUNTANT,
                ),
                CollectionAction(
                    account_id=account.id,
                    kind="call",
                    channel="phone",
                    note="Arandı; ay sonuna kadar ödeyeceğini söyledi",
                    by_name=MANAGER,
                ),
            ]
        )
        if index == 0:
            letter = await debt_tracking.add_letter(
                session,
                site,
                account,
                method="registered_mail",
                sent_on=today - timedelta(days=12),
                tracking_no="RR123456789TR",
                note="Demo ihtarname",
                actor=_actor(MANAGER),
                today=today,
                now=_at(today, 11),
            )
            await debt_tracking.mark_served(
                session, letter.letter, today - timedelta(days=5), today
            )
    await session.flush()


async def _ad_hoc(session: AsyncSession, site: Site, today: date) -> None:
    accounts = list(
        await session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.kind == "occupant", LedgerAccount.is_closed.is_(False))
            .order_by(LedgerAccount.reference_code)
            .limit(4)
        )
    )
    if len(accounts) < 4:
        return
    await ad_hoc_charges.save_settings(
        session, {"renovation": {"mode": "next_period", "due_days": 10}}
    )
    specs = [
        ("penalty", "Otopark kuralı ihlali cezası", "250.00"),
        ("service", "Halı yıkama hizmeti", "180.00"),
        ("renovation", "Tadilat izni ve asansör koruma", "400.00"),
        ("other", "Kayıp anahtar kartı", "75.00"),
    ]
    created = []
    for account, (source, text, amount) in zip(accounts, specs, strict=True):
        result = await ad_hoc_charges.create(
            session,
            site,
            ad_hoc_charges.NewCharge(
                account.unit_id, "occupant", source, text, Decimal(amount), None
            ),
            actor=_actor(MANAGER),
            today=today,
            now=_at(today, 9),
            manual=False,
        )
        created.append(result.charge)
    await ad_hoc_charges.cancel(
        session, created[-1], reason="Kart bulundu; ücret iptal", today=today, now=_at(today, 12)
    )
