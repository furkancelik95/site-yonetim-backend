"""Rezervasyon — alanlar ve kurallar, rezervasyon (sakin ve sakin adına), iptal, bekleme listesi,
kullanım doğrulama, no-show ve kullanım raporu (Yazılım Dokümanı §14, REZ-01..03; servis
istekleri 59–61). Açık site kapsamında; commit çağırana ait.

Çakışma (REZ-02 S1): yazan her işlem ortak alan satırını `FOR UPDATE` kilitler; aynı alana
eşzamanlı iki istekten ikincisi birincinin sonucunu görür. Veritabanında ayrıca kısmi benzersiz
indeksler (yer + başlangıç, bölüm + başlangıç) ve — `btree_gist` varsa — zaman aralığı
`EXCLUDE` kısıtı vardır; kısıt ihlali 409 `slot_full` olur.

Ücret: onaylanınca cari deftere (`reservation` kaynağı, kiracı/oturan hesabı yoksa malik
hesabı). GEÇİCİ: dönem dışı ücret servisi (servis isteği 31, P03a) gelince yazım oradaki
`ad_hoc_charges` iç servisine taşınmalı (yansıma kuralı oradan okunur). İptalde son iptal
süresinden önce ters kayıt; sonra `late_cancel_refund` kapalıysa iade yok. No-show'da iade yok.
"""

import secrets
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType, ApprovalStatus, Decision
from site_yonetim.domain.charging.periods import DUE_AFTER_DAYS
from site_yonetim.domain.finance import LedgerSource
from site_yonetim.domain.invitations import HouseholdStatus
from site_yonetim.domain.money import EPSILON, ZERO, round_money
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.reservations import (
    CODE_LENGTH,
    DEFAULT_RULES,
    LIVE_VALUES,
    OPEN_WAIT,
    REPORT_DAYS,
    USED,
    WEEKLY_COUNTED,
    ApprovalMode,
    BookingChannel,
    BookingInput,
    ChannelRule,
    DebtRule,
    FeeType,
    NoShowRule,
    ReservationStatus,
    Rules,
    VerifyMethod,
    WaitlistStatus,
    blocked_message,
    blocked_until,
    check_booking,
    check_code,
    check_debt_rule,
    check_rules,
    close_status,
    debt_blocked,
    debt_message,
    fee_of,
    hm,
    is_late,
    local,
    no_show_rate,
    noshow_window_start,
    occupancy,
    slots,
    week_start,
)
from site_yonetim.domain.service_desk import RuleError
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import format_date_tr, format_money_tr
from site_yonetim.models import (
    AccountBalance,
    ApprovalDecision,
    CommonArea,
    HouseholdMember,
    LedgerAccount,
    LedgerEntry,
    Reservation,
    ReservationRules,
    ReservationSettings,
    ReservationWaitlist,
    Site,
    UnitParty,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import ledger, service_desk
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.paid_services import billing_account

REASON_MIN, REASON_MAX, NAME_MAX = 5, 300, 60
JOB_ACTOR = "Rezervasyon zamanlayıcısı"
AREA_MISSING = "Rezerve edilebilir alan bulunamadı (Yapı → Ortak alanlar'da işaretleyin)."
_LIVE = list(LIVE_VALUES)


def _conflict(code: str, message: str) -> OperationRuleError:
    return OperationRuleError(code, message, conflict=True)


def local_now(now: datetime) -> datetime:
    return now.astimezone(BUSINESS_TZ)


# --- Alanlar ve kurallar (REZ-01) ------------------------------------------------------------


def _rules_of(row: ReservationRules | None) -> Rules:
    if row is None:
        return DEFAULT_RULES
    return Rules(
        enabled=row.enabled,
        open_from=row.open_from,
        open_to=row.open_to,
        slot_minutes=row.slot_minutes,
        capacity=row.capacity,
        max_people=row.max_people,
        max_guests=row.max_guests,
        weekly_limit=row.weekly_limit,
        cancel_hours=row.cancel_hours,
        late_cancel_refund=row.late_cancel_refund,
        fee_type=FeeType(row.fee_type),
        fee_amount=row.fee_amount,
        channel=ChannelRule(row.channel),
        approval=ApprovalMode(row.approval),
        verify=VerifyMethod(row.verify),
        waitlist=row.waitlist,
        waitlist_minutes=row.waitlist_minutes,
        noshow=NoShowRule(
            row.noshow_enabled, row.noshow_months, row.noshow_count, row.noshow_block_days
        ),
    )


def _reservable() -> Select[CommonArea]:
    return select(CommonArea).where(CommonArea.is_reservable, CommonArea.is_active)


async def areas(session: AsyncSession) -> list[CommonArea]:
    return list(await session.scalars(_reservable().order_by(CommonArea.name)))


async def area(
    session: AsyncSession, area_id: uuid.UUID, *, lock: bool = False
) -> CommonArea | None:
    """Rezerve edilebilir ve etkin alan. `lock`: alan satırı kilitlenir (yazma işlemleri)."""
    query = _reservable().where(CommonArea.id == area_id)
    found: CommonArea | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def _lock_area(session: AsyncSession, area_id: uuid.UUID) -> None:
    await session.execute(select(CommonArea.id).where(CommonArea.id == area_id).with_for_update())


async def rules_for(session: AsyncSession, area_ids: list[uuid.UUID]) -> dict[uuid.UUID, Rules]:
    rows = {
        r.area_id: r
        for r in await session.scalars(
            select(ReservationRules).where(ReservationRules.area_id.in_(area_ids))
        )
    } if area_ids else {}  # fmt: skip
    return {i: _rules_of(rows.get(i)) for i in area_ids}


async def rules_of(session: AsyncSession, area_id: uuid.UUID) -> Rules:
    return (await rules_for(session, [area_id]))[area_id]


async def save_rules(session: AsyncSession, target: CommonArea, raw: dict[str, object]) -> Rules:
    rules = check_rules(raw)
    row = await session.scalar(
        select(ReservationRules).where(ReservationRules.area_id == target.id)
    )
    if row is None:
        row = ReservationRules(area_id=target.id)
        session.add(row)
    row.enabled, row.open_from, row.open_to = rules.enabled, rules.open_from, rules.open_to
    row.slot_minutes, row.capacity = rules.slot_minutes, rules.capacity
    row.max_people, row.max_guests = rules.max_people, rules.max_guests
    row.weekly_limit, row.cancel_hours = rules.weekly_limit, rules.cancel_hours
    row.late_cancel_refund = rules.late_cancel_refund
    row.fee_type, row.fee_amount = rules.fee_type.value, rules.fee_amount
    row.channel, row.approval = rules.channel.value, rules.approval.value
    row.verify = rules.verify.value
    row.waitlist, row.waitlist_minutes = rules.waitlist, rules.waitlist_minutes
    row.noshow_enabled, row.noshow_months = rules.noshow.enabled, rules.noshow.months
    row.noshow_count, row.noshow_block_days = rules.noshow.count, rules.noshow.block_days
    await session.flush()
    return rules


# --- K11 ayarı ----------------------------------------------------------------------------


async def debt_rule(session: AsyncSession) -> DebtRule:
    row = await session.scalar(select(ReservationSettings))
    if row is None:
        return DebtRule()
    return DebtRule(row.debt_block_enabled, row.debt_min_amount, row.debt_min_days)


async def save_debt_rule(
    session: AsyncSession, enabled: bool, min_amount: Decimal | None, min_days: int | None
) -> DebtRule:
    rule = check_debt_rule(enabled, min_amount, min_days)
    row = await session.scalar(select(ReservationSettings))
    if row is None:
        row = ReservationSettings()
        session.add(row)
    row.debt_block_enabled = rule.enabled
    row.debt_min_amount, row.debt_min_days = rule.min_amount, rule.min_days
    await session.flush()
    return rule


async def unit_debt(session: AsyncSession, unit_id: uuid.UUID) -> tuple[Decimal, date | None]:
    """Bölümün açık hesaplarındaki borç toplamı ve en eski açık borcun vadesi (özet tablodan)."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(AccountBalance.balance), ZERO),
                func.min(AccountBalance.oldest_open_due_date),
            )
            .join(
                LedgerAccount,
                and_(
                    LedgerAccount.id == AccountBalance.account_id,
                    LedgerAccount.site_id == AccountBalance.site_id,
                ),
            )
            .where(
                LedgerAccount.unit_id == unit_id,
                LedgerAccount.is_closed.is_(False),
                AccountBalance.balance > EPSILON,
            )
        )
    ).one()
    return row[0], row[1]


async def debt_block(session: AsyncSession, unit_id: uuid.UUID, today: date) -> str | None:
    """K11 açık ve eşik aşılmışsa gerekçe; değilse None."""
    rule = await debt_rule(session)
    if not rule.enabled:
        return None
    balance, oldest = await unit_debt(session, unit_id)
    return debt_message(rule, balance) if debt_blocked(rule, balance, oldest, today) else None


# --- Sakin bölümü ve hane üyesi yetkisi ----------------------------------------------------


async def resident_units(
    session: AsyncSession, person_id: uuid.UUID, today: date
) -> list[uuid.UUID]:
    """Kişinin bugün malik/kiracı/oturan olduğu bölümler (vekil hariç), ad sırasıyla."""
    ids = await requests_svc.person_unit_ids(session, person_id, today)
    names = await requests_svc.unit_names(session, ids)
    return sorted(ids, key=lambda i: names.get(i, ""))


async def may_reserve(
    session: AsyncSession, person_id: uuid.UUID, unit_id: uuid.UUID, today: date
) -> bool:
    """Malik/kiracı rezervasyon yapar; hane üyesi yalnız davet edenin verdiği "Rezervasyon"
    yetkisiyle (REZ-02 adım 2; Ürün Dokümanı hane üyesi yetkileri — varsayılan kapalı)."""
    roles = set(
        await session.scalars(
            select(UnitParty.role).where(
                UnitParty.person_id == person_id,
                UnitParty.unit_id == unit_id,
                UnitParty.start_date <= today,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
            )
        )
    )
    if roles & {PartyRole.OWNER.value, PartyRole.TENANT.value}:
        return True
    member = await session.scalar(
        select(HouseholdMember.can_reservations).where(
            HouseholdMember.person_id == person_id,
            HouseholdMember.unit_id == unit_id,
            HouseholdMember.status == HouseholdStatus.ACTIVE.value,
        )
    )
    if member is not None:
        return bool(member)
    return PartyRole.RESIDENT.value in roles


async def _unit_users(session: AsyncSession, unit_id: uuid.UUID, today: date) -> set[uuid.UUID]:
    people = set(
        await session.scalars(
            select(UnitParty.person_id).where(
                UnitParty.unit_id == unit_id,
                UnitParty.role != PartyRole.PROXY.value,
                UnitParty.start_date <= today,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
            )
        )
    )
    return await requests_svc.person_user_ids(session, people)


async def _booker_users(
    session: AsyncSession, person_id: uuid.UUID | None, unit_id: uuid.UUID, today: date
) -> set[uuid.UUID]:
    """Bildirim alıcısı: rezervasyonu sakin yaptıysa o; yönetim açtıysa bölümün sakinleri."""
    if person_id is not None:
        return await requests_svc.person_user_ids(session, {person_id})
    return await _unit_users(session, unit_id, today)


async def _notify(
    session: AsyncSession,
    users: set[uuid.UUID],
    *,
    kind: str,
    title: str,
    body: str | None,
    link_id: uuid.UUID,
    key: str,
) -> None:
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=body,
            link_entity="reservations",
            link_id=link_id,
        ),
        dedupe_key=key,
    )


async def area_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(CommonArea.id, CommonArea.name).where(CommonArea.id.in_(ids))
    )
    return {row[0]: row[1] for row in rows.all()}


def _when(r: Reservation | ReservationWaitlist) -> str:
    return f"{format_date_tr(r.date)} {hm(r.start_time)}–{hm(r.end_time)}"


# --- Doluluk ---------------------------------------------------------------------------------


def _overlaps(
    model: type[Reservation] | type[ReservationWaitlist], start: datetime, end: datetime
) -> ColumnElement[bool]:
    return and_(model.starts_at < end, model.ends_at > start)


async def _live(
    session: AsyncSession, area_id: uuid.UUID, start: datetime, end: datetime
) -> list[Reservation]:
    return list(
        await session.scalars(
            select(Reservation).where(
                Reservation.area_id == area_id,
                Reservation.status.in_(_LIVE),
                _overlaps(Reservation, start, end),
            )
        )
    )


async def _active_offers(
    session: AsyncSession,
    area_id: uuid.UUID,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    exclude: uuid.UUID | None = None,
) -> int:
    query = select(func.count()).where(
        ReservationWaitlist.area_id == area_id,
        ReservationWaitlist.status == WaitlistStatus.OFFERED.value,
        ReservationWaitlist.offered_until > now,
        _overlaps(ReservationWaitlist, start, end),
    )
    if exclude is not None:
        query = query.where(ReservationWaitlist.id != exclude)
    return await session.scalar(query) or 0


@dataclass(frozen=True, slots=True)
class SlotState:
    start: time
    end: time
    capacity: int
    taken: int
    past: bool
    mine: bool
    waiting: int


async def availability(
    session: AsyncSession,
    area_id: uuid.UUID,
    rules: Rules,
    day: date,
    *,
    now: datetime,
    unit_ids: set[uuid.UUID] | None = None,
) -> list[SlotState]:
    """Saat ızgarası: dolu = canlı rezervasyon + süresi geçmemiş teklif (REZ-02 madde 4)."""
    grid = slots(rules)
    if not grid:
        return []
    day_start = local(day, time(0, 0), BUSINESS_TZ)
    day_end = day_start + timedelta(days=1)
    live = await _live(session, area_id, day_start, day_end)
    waits = list(
        await session.scalars(
            select(ReservationWaitlist).where(
                ReservationWaitlist.area_id == area_id,
                ReservationWaitlist.status.in_([s.value for s in OPEN_WAIT]),
                _overlaps(ReservationWaitlist, day_start, day_end),
            )
        )
    )
    now_l = local_now(now)
    out: list[SlotState] = []
    for start, end in grid:
        s, e = local(day, start, BUSINESS_TZ), local(day, end, BUSINESS_TZ)
        here = [r for r in live if r.starts_at < e and r.ends_at > s]
        offers = [
            w for w in waits
            if w.status == WaitlistStatus.OFFERED.value and w.offered_until is not None
            and w.offered_until > now and w.starts_at < e and w.ends_at > s
        ]  # fmt: skip
        out.append(
            SlotState(
                start=start,
                end=end,
                capacity=rules.capacity,
                taken=len(here) + len(offers),
                past=day < now_l.date() or (day == now_l.date() and start <= now_l.time()),
                mine=bool(unit_ids) and any(r.unit_id in (unit_ids or set()) for r in here),
                waiting=sum(
                    1
                    for w in waits
                    if w.status == WaitlistStatus.WAITING.value and w.starts_at == s
                ),
            )
        )
    return out


async def noshow_until(
    session: AsyncSession, rules: Rules, unit_id: uuid.UUID, area_id: uuid.UUID, today: date
) -> date | None:
    if not rules.noshow.enabled or rules.verify is VerifyMethod.NONE:
        return None
    dates = list(
        await session.scalars(
            select(Reservation.date).where(
                Reservation.unit_id == unit_id,
                Reservation.area_id == area_id,
                Reservation.status == ReservationStatus.NO_SHOW.value,
                Reservation.date >= noshow_window_start(rules, today),
            )
        )
    )
    return blocked_until(rules, dates, today)


# --- Ücret ------------------------------------------------------------------------------------


async def _bill(session: AsyncSession, r: Reservation, area_name: str, today: date) -> None:
    if r.fee <= 0 or r.charge_entry_id is not None:
        return
    if r.account_id is None:
        account = await billing_account(session, r.unit_id)
        if account is None:
            raise _conflict(
                "no_account", "Bu bölümde açık cari hesap yok; ücret yazılamaz. Önce bölüme "
                "malik/kiracı ekleyin.",
            )  # fmt: skip
        r.account_id = account.id
    await ledger.lock_accounts(session, [r.account_id])
    entry = LedgerEntry(
        id=uuid.uuid7(),
        account_id=r.account_id,
        date=today,
        due_date=today + timedelta(days=DUE_AFTER_DAYS),
        debit=r.fee,
        source=LedgerSource.RESERVATION.value,
        source_id=r.id,
        description=f"Rezervasyon — {area_name} {_when(r)}"[:200],
    )
    session.add(entry)
    await session.flush()
    await ledger.refresh_balances(session, [r.account_id])
    r.charge_entry_id = entry.id


async def _refund(session: AsyncSession, r: Reservation, today: date) -> None:
    if r.charge_entry_id is None or r.refund_entry_id is not None:
        return
    original = await session.get(LedgerEntry, r.charge_entry_id)
    if original is None:  # pragma: no cover
        return
    await ledger.lock_accounts(session, [original.account_id])
    refund = LedgerEntry(
        id=uuid.uuid7(),
        account_id=original.account_id,
        date=today,
        credit=original.debit,
        source=LedgerSource.RESERVATION.value,
        source_id=r.id,
        description=f"Rezervasyon iptali (ters kayıt) — {original.description}"[:200],
        reversal_of_entry_id=original.id,
    )
    session.add(refund)
    await session.flush()
    await ledger.refresh_balances(session, [original.account_id])
    r.refund_entry_id = refund.id


# --- Rezervasyon oluşturma (REZ-02) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Booker:
    unit_id: uuid.UUID
    person_id: uuid.UUID | None  # sakin; personel sakin adına açtıysa None
    person_name: str
    channel: BookingChannel
    user: approvals_svc.Person  # işlemi yapan (sakin ya da personel)


@dataclass(slots=True)
class Booking:
    reservation: Reservation | None = None
    waitlist: ReservationWaitlist | None = None
    position: int | None = None
    approval: ApprovalRequest | None = None
    messages: list[str] = field(default_factory=list)


async def _new_code(session: AsyncSession, day: date) -> str:
    used = set(
        await session.scalars(
            select(Reservation.check_in_code).where(
                Reservation.date == day, Reservation.status.in_(_LIVE)
            )
        )
    )
    while True:
        code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
        if code not in used:
            return code


async def _free_slots(
    session: AsyncSession, area_id: uuid.UUID, rules: Rules, day: date, now: datetime
) -> list[str]:
    grid = await availability(session, area_id, rules, day, now=now)
    return [hm(s.start) for s in grid if not s.past and s.taken < s.capacity][:4]


async def create(
    session: AsyncSession,
    site: Site,
    area_id: uuid.UUID | None,
    data: BookingInput,
    booker: Booker,
    *,
    want_waitlist: bool,
    now: datetime,
    today: date,
    from_offer: ReservationWaitlist | None = None,
) -> Booking:
    """Belge akışındaki sırayla: kanal → tarih/saat/kişi → K11 → no-show → sınır → çakışma →
    bekleme listesi → onay → ücret."""
    target = await area(session, area_id, lock=True) if area_id else None
    if target is None:
        raise RuleError({"area_id": "Alan seçin."})
    rules = await rules_of(session, target.id)
    if not rules.enabled:
        raise _conflict("area_closed", f"{target.name} rezervasyona kapalı.")
    if booker.channel is BookingChannel.RESIDENT and rules.channel is ChannelRule.STAFF:
        raise _conflict(
            "channel", f"{target.name} için rezervasyonu yönetim açar; lütfen yönetimi arayın."
        )
    if booker.channel is BookingChannel.STAFF and rules.channel is ChannelRule.RESIDENT:
        raise _conflict("channel", f"{target.name} için rezervasyonu sakin kendisi yapar.")
    day, start, end, people, guests = check_booking(rules, data, now_local=local_now(now))
    starts_at, ends_at = local(day, start, BUSINESS_TZ), local(day, end, BUSINESS_TZ)
    if (reason := await debt_block(session, booker.unit_id, today)) is not None:
        raise _conflict("debt_restricted", reason)
    until = await noshow_until(session, rules, booker.unit_id, target.id, today)
    if until is not None:
        raise _conflict("noshow_blocked", blocked_message(target.name, until))
    if rules.weekly_limit > 0:
        ws = week_start(day)
        count = await session.scalar(
            select(func.count()).where(
                Reservation.unit_id == booker.unit_id,
                Reservation.area_id == target.id,
                Reservation.status.in_([s.value for s in WEEKLY_COUNTED]),
                Reservation.date.between(ws, ws + timedelta(days=6)),
            )
        ) or 0  # fmt: skip
        if count >= rules.weekly_limit:
            raise _conflict(
                "limit",
                f"Haftalık sınır aşıldı: bölüm başına haftada {rules.weekly_limit} rezervasyon "
                f"({target.name}).",
            )
    live = await _live(session, target.id, starts_at, ends_at)
    if any(r.unit_id == booker.unit_id for r in live):
        raise _conflict("duplicate", "Bu saat için bölümün rezervasyonu zaten var.")
    offers = await _active_offers(
        session, target.id, starts_at, ends_at, now,
        exclude=from_offer.id if from_offer else None,
    )  # fmt: skip
    if len(live) + offers >= rules.capacity:
        if rules.waitlist and want_waitlist and from_offer is None:
            return await _join_waitlist(
                session, target, rules, booker, day, start, end, people, guests, now=now
            )
        free = await _free_slots(session, target.id, rules, day, now)
        message = "Bu saat dolu."
        message += f" Boş saatler: {', '.join(free)}." if free else " Bu gün boş saat yok."
        if rules.waitlist and from_offer is None:
            message += " Bekleme listesine girebilirsiniz."
        raise _conflict("slot_full", message)
    taken_seats = {r.seat for r in live}
    seat = next(i for i in range(1, rules.capacity + 1) if i not in taken_seats)
    fee = fee_of(rules, guests)
    account = await billing_account(session, booker.unit_id)
    if fee > 0 and account is None:
        raise _conflict(
            "no_account",
            "Bu bölümde açık cari hesap yok; ücretli rezervasyon açılamaz. Yönetimle görüşün.",
        )
    auto = rules.approval is ApprovalMode.AUTO
    reservation = Reservation(
        id=uuid.uuid7(),
        area_id=target.id,
        unit_id=booker.unit_id,
        person_id=booker.person_id,
        account_id=account.id if account else None,
        person_name=" ".join(booker.person_name.split())[:NAME_MAX] or "—",
        date=day,
        start_time=start,
        end_time=end,
        starts_at=starts_at,
        ends_at=ends_at,
        seat=seat,
        people=people,
        guests=guests,
        status=(ReservationStatus.APPROVED if auto else ReservationStatus.PENDING).value,
        check_in_code=await _new_code(session, day),
        channel=booker.channel.value,
        created_by_name=booker.user.name,
        created_by_user_id=booker.user.user_id,
        fee=fee,
        decided_by_name="Otomatik" if auto else None,
        decided_at=now if auto else None,
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError as exc:
        raise _conflict("slot_full", "Bu saat az önce doldu; başka bir saat seçin.") from exc
    result = Booking(reservation=reservation)
    unit_name = (await requests_svc.unit_names(session, {booker.unit_id})).get(booker.unit_id, "")
    if auto:
        await _bill(session, reservation, target.name, today)
        result.messages.append("Rezervasyon onaylandı.")
        if reservation.charge_entry_id is not None:
            result.messages.append(f"Ücret {format_money_tr(fee)} borca yansıdı.")
    else:
        rule = await approvals_svc.match_rule(
            session, ActionType.RESERVATION, amount=fee if fee > 0 else None, source=None,
            today=today,
        )  # fmt: skip
        if rule is not None:
            result.approval = await approvals_svc.request_approval(
                session,
                site,
                approvals_svc.NewRequest(
                    action_type=ActionType.RESERVATION,
                    title=f"Rezervasyon — {target.name}, {unit_name} {_when(reservation)}",
                    summary={
                        "area_name": target.name,
                        "unit_name": unit_name,
                        "person_name": reservation.person_name,
                        "date": day.isoformat(),
                        "start": hm(start),
                        "end": hm(end),
                        "people": people,
                        "guests": guests,
                        "fee": str(fee),
                    },
                    amount=fee if fee > 0 else None,
                    subject_type="reservations",
                    subject_id=reservation.id,
                ),
                requester=booker.user,
                today=today,
                now=now,
            )
            reservation.approval_request_id = result.approval.id
            await session.flush()
            result.messages.append("Rezervasyon alındı; onay kuralına göre onaycılara gönderildi.")
        else:
            await _notify_pending_staff(
                session, site, reservation, target.name, unit_name,
            )  # fmt: skip
            result.messages.append("Rezervasyon alındı; yönetici onayı bekleniyor.")
    if booker.channel is BookingChannel.STAFF:
        users = await _unit_users(session, booker.unit_id, today)
        await _notify(
            session, users, kind="reservation.created",
            title=f"Adınıza rezervasyon açıldı: {target.name}",
            body=f"{_when(reservation)} — {booker.user.name} açtı."
            + ("" if auto else " Yönetici onayı bekleniyor."),
            link_id=reservation.id, key=f"reservation:{reservation.id}:created",
        )  # fmt: skip
        result.messages.append("Sakine bildirim gitti.")
    return result


async def _notify_pending_staff(
    session: AsyncSession, site: Site, r: Reservation, area_name: str, unit_name: str
) -> None:
    """Onay kuralı yoksa onay Rezervasyon ekranından verilir: yetkili personele bildirim."""
    users = await service_desk.staff_user_ids(session, site, Permission.REQUESTS_ASSIGN)
    await _notify(
        session, users, kind="reservation.pending",
        title=f"Onay bekleyen rezervasyon: {area_name}",
        body=f"{unit_name} · {_when(r)}", link_id=r.id, key=f"reservation:{r.id}:pending",
    )  # fmt: skip


async def _join_waitlist(
    session: AsyncSession,
    target: CommonArea,
    rules: Rules,
    booker: Booker,
    day: date,
    start: time,
    end: time,
    people: int,
    guests: int,
    *,
    now: datetime,
) -> Booking:
    starts_at = local(day, start, BUSINESS_TZ)
    exists = await session.scalar(
        select(ReservationWaitlist.id).where(
            ReservationWaitlist.area_id == target.id,
            ReservationWaitlist.unit_id == booker.unit_id,
            ReservationWaitlist.starts_at == starts_at,
            ReservationWaitlist.status.in_([s.value for s in OPEN_WAIT]),
        )
    )
    if exists is not None:
        raise _conflict("already_waiting", "Bu saat için zaten bekleme listesindesiniz.")
    entry = ReservationWaitlist(
        id=uuid.uuid7(),
        area_id=target.id,
        unit_id=booker.unit_id,
        person_id=booker.person_id,
        person_name=" ".join(booker.person_name.split())[:NAME_MAX] or "—",
        created_by_user_id=booker.user.user_id,
        date=day,
        start_time=start,
        end_time=end,
        starts_at=starts_at,
        ends_at=local(day, end, BUSINESS_TZ),
        people=people,
        guests=guests,
        status=WaitlistStatus.WAITING.value,
    )
    session.add(entry)
    await session.flush()
    position = await session.scalar(
        select(func.count()).where(
            ReservationWaitlist.area_id == target.id,
            ReservationWaitlist.starts_at == starts_at,
            ReservationWaitlist.status == WaitlistStatus.WAITING.value,
            ReservationWaitlist.id <= entry.id,
        )
    ) or 1  # fmt: skip
    return Booking(
        waitlist=entry,
        position=position,
        messages=[
            f"Bekleme listesine eklendiniz; sıranız {position}. Yer açılınca "
            f"{rules.waitlist_minutes} dakika geçerli teklif gelir."
        ],
    )


# --- Bekleme listesi teklifi -----------------------------------------------------------------


async def offer_next(
    session: AsyncSession, area_id: uuid.UUID, starts_at: datetime, ends_at: datetime,
    *, now: datetime,
) -> list[ReservationWaitlist]:  # fmt: skip
    """Yer boşaldıysa sıradakilere `waitlist_minutes` geçerli teklif. Alan kilitli olmalı."""
    rules = await rules_of(session, area_id)
    if not rules.waitlist or starts_at <= now:
        return []
    offered: list[ReservationWaitlist] = []
    while True:
        taken = len(await _live(session, area_id, starts_at, ends_at))
        held = await _active_offers(session, area_id, starts_at, ends_at, now)
        if taken + held >= rules.capacity:
            return offered
        nxt = await session.scalar(
            select(ReservationWaitlist)
            .where(
                ReservationWaitlist.area_id == area_id,
                ReservationWaitlist.starts_at == starts_at,
                ReservationWaitlist.status == WaitlistStatus.WAITING.value,
            )
            .order_by(ReservationWaitlist.created_at, ReservationWaitlist.id)
            .limit(1)
            .with_for_update()
        )
        if nxt is None:
            return offered
        nxt.status = WaitlistStatus.OFFERED.value
        nxt.offered_at = now
        nxt.offered_until = now + timedelta(minutes=rules.waitlist_minutes)
        await session.flush()
        name = (await area_names(session, {area_id})).get(area_id, "")
        await _notify(
            session, await _booker_users(session, nxt.person_id, nxt.unit_id, now.date()),
            kind="reservation.waitlist_offer", title=f"Yer açıldı: {name}",
            body=f"{_when(nxt)} — {rules.waitlist_minutes} dakika içinde onaylayın.",
            link_id=nxt.id, key=f"waitlist:{nxt.id}:offer",
        )  # fmt: skip
        offered.append(nxt)


async def get_waitlist(
    session: AsyncSession, entry_id: uuid.UUID, *, unit_ids: set[uuid.UUID] | None = None
) -> ReservationWaitlist | None:
    query = select(ReservationWaitlist).where(ReservationWaitlist.id == entry_id)
    if unit_ids is not None:
        query = query.where(ReservationWaitlist.unit_id.in_(unit_ids))
    found: ReservationWaitlist | None = await session.scalar(query.with_for_update())
    return found


async def accept_offer(
    session: AsyncSession, site: Site, entry: ReservationWaitlist, booker: Booker,
    *, now: datetime, today: date,
) -> Booking:  # fmt: skip
    await _lock_area(session, entry.area_id)
    expired = entry.status == WaitlistStatus.EXPIRED.value or (
        entry.status == WaitlistStatus.OFFERED.value
        and entry.offered_until is not None and entry.offered_until <= now
    )  # fmt: skip
    if expired:
        raise _conflict("expired", "Teklifin süresi doldu; sıradaki kişiye geçti.")
    if entry.status != WaitlistStatus.OFFERED.value:
        raise _conflict("invalid_status", "Bu kayıt için teklif yok.")
    async with session.begin_nested():
        result = await create(
            session, site, entry.area_id,
            BookingInput(entry.date, entry.start_time, entry.people, entry.guests), booker,
            want_waitlist=False, now=now, today=today, from_offer=entry,
        )  # fmt: skip
        entry.status = WaitlistStatus.ACCEPTED.value
        entry.closed_at = now
        entry.reservation_id = result.reservation.id if result.reservation else None
        await session.flush()
    return result


async def leave_waitlist(
    session: AsyncSession, entry: ReservationWaitlist, *, now: datetime
) -> ReservationWaitlist:
    if WaitlistStatus(entry.status) not in OPEN_WAIT:
        raise _conflict("invalid_status", "Bu bekleme kaydı kapanmış.")
    await _lock_area(session, entry.area_id)
    was_offered = entry.status == WaitlistStatus.OFFERED.value
    entry.status = WaitlistStatus.LEFT.value
    entry.closed_at = now
    await session.flush()
    if was_offered:
        await offer_next(session, entry.area_id, entry.starts_at, entry.ends_at, now=now)
    return entry


# --- Karar, iptal, giriş --------------------------------------------------------------------


async def get(
    session: AsyncSession, reservation_id: uuid.UUID, *, unit_ids: set[uuid.UUID] | None = None
) -> Reservation | None:
    query = select(Reservation).where(Reservation.id == reservation_id)
    if unit_ids is not None:
        query = query.where(Reservation.unit_id.in_(unit_ids))
    found: Reservation | None = await session.scalar(query.with_for_update())
    return found


async def approval_status(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ApprovalStatus]:
    if not ids:
        return {}
    rows = await session.execute(
        select(ApprovalRequest.id, ApprovalRequest.status).where(ApprovalRequest.id.in_(ids))
    )
    return {i: ApprovalStatus(s) for i, s in rows.all()}


async def _not_in_approval(session: AsyncSession, r: Reservation) -> None:
    if r.status != ReservationStatus.PENDING.value:
        raise _conflict("invalid_status", "Yalnız onay bekleyen rezervasyon karara bağlanır.")
    if r.approval_request_id is not None:
        current = (await approval_status(session, [r.approval_request_id])).get(
            r.approval_request_id
        )
        if current in OPEN_STATUSES:
            raise _conflict(
                "approval_pending",
                "Bu rezervasyon onay kuralına bağlı; kararı Onaylar ekranından verin.",
            )


async def _approve(
    session: AsyncSession, r: Reservation, *, actor: str, now: datetime, today: date
) -> None:
    name = (await area_names(session, {r.area_id})).get(r.area_id, "")
    r.status = ReservationStatus.APPROVED.value
    r.decided_by_name, r.decided_at = actor, now
    await _bill(session, r, name, today)
    await session.flush()
    body = _when(r)
    if r.charge_entry_id is not None:
        body += f" — ücret {format_money_tr(r.fee)} borcunuza yansıdı."
    await _notify(
        session, await _booker_users(session, r.person_id, r.unit_id, today),
        kind="reservation.approved", title=f"Rezervasyonunuz onaylandı: {name}", body=body,
        link_id=r.id, key=f"reservation:{r.id}:approved",
    )  # fmt: skip


async def approve(
    session: AsyncSession, r: Reservation, *, actor: str, now: datetime, today: date
) -> Reservation:
    await _not_in_approval(session, r)
    await _approve(session, r, actor=actor, now=now, today=today)
    return r


async def _reject(
    session: AsyncSession, r: Reservation, reason: str, *, actor: str, now: datetime
) -> list[ReservationWaitlist]:
    await _lock_area(session, r.area_id)
    r.status = ReservationStatus.REJECTED.value
    r.reject_reason = reason[:REASON_MAX]
    r.decided_by_name, r.decided_at = actor, now
    await session.flush()
    name = (await area_names(session, {r.area_id})).get(r.area_id, "")
    await _notify(
        session, await _booker_users(session, r.person_id, r.unit_id, now.date()),
        kind="reservation.rejected", title=f"Rezervasyonunuz reddedildi: {name}",
        body=f"{_when(r)} — gerekçe: {r.reject_reason}", link_id=r.id,
        key=f"reservation:{r.id}:rejected",
    )  # fmt: skip
    return await offer_next(session, r.area_id, r.starts_at, r.ends_at, now=now)


async def reject(
    session: AsyncSession, r: Reservation, reason: str | None, *, actor: str, now: datetime
) -> list[ReservationWaitlist]:
    await _not_in_approval(session, r)
    text = " ".join((reason or "").split())
    if len(text) < REASON_MIN:
        raise RuleError({"reason": "Ret gerekçesi yazın."})
    return await _reject(session, r, text, actor=actor, now=now)


@dataclass(frozen=True, slots=True)
class Cancelled:
    refunded: bool
    late: bool
    offers: list[ReservationWaitlist]


async def cancel(
    session: AsyncSession,
    r: Reservation,
    reason: str | None,
    *,
    actor: str,
    by_staff: bool,
    now: datetime,
    today: date,
) -> Cancelled:
    if ReservationStatus(r.status) not in {ReservationStatus.PENDING, ReservationStatus.APPROVED}:
        raise _conflict("invalid_status", "Bu rezervasyon iptal edilemez.")
    await _lock_area(session, r.area_id)
    rules = await rules_of(session, r.area_id)
    late = is_late(r.starts_at, rules, now)
    r.status = ReservationStatus.CANCELLED.value
    r.cancelled_late, r.cancelled_at = late, now
    text = " ".join((reason or "").split())[:REASON_MAX]
    r.cancel_reason = text or (f"Yönetim ({actor})" if by_staff else "Sakin iptal etti")
    refunded = False
    if r.charge_entry_id is not None and (not late or rules.late_cancel_refund):
        await _refund(session, r, today)
        refunded = True
    await session.flush()
    if by_staff:
        name = (await area_names(session, {r.area_id})).get(r.area_id, "")
        await _notify(
            session, await _booker_users(session, r.person_id, r.unit_id, today),
            kind="reservation.cancelled", title=f"Rezervasyonunuz iptal edildi: {name}",
            body=f"{_when(r)} — {r.cancel_reason}", link_id=r.id,
            key=f"reservation:{r.id}:cancelled",
        )  # fmt: skip
    offers = await offer_next(session, r.area_id, r.starts_at, r.ends_at, now=now)
    return Cancelled(refunded, late, offers)


async def check_in(
    session: AsyncSession, r: Reservation, *, actor: str, now: datetime
) -> Reservation:
    if r.status == ReservationStatus.CHECKED_IN.value:
        raise _conflict("already", "Giriş zaten kaydedildi.")
    if r.status != ReservationStatus.APPROVED.value:
        raise _conflict("invalid_status", "Yalnız onaylı rezervasyona giriş yapılır.")
    if r.date != local_now(now).date():
        raise _conflict("not_today", "Rezervasyon bugün için değil.")
    r.status = ReservationStatus.CHECKED_IN.value
    r.checked_in_at, r.checked_in_by_name = now, actor
    await session.flush()
    return r


async def check_in_code(
    session: AsyncSession, raw: str | None, *, actor: str, now: datetime
) -> Reservation:
    code = check_code(raw)
    r: Reservation | None = await session.scalar(
        select(Reservation)
        .where(
            Reservation.check_in_code == code,
            Reservation.date == local_now(now).date(),
            Reservation.status.in_(
                [ReservationStatus.APPROVED.value, ReservationStatus.CHECKED_IN.value]
            ),
        )
        .with_for_update()
    )
    if r is None:
        raise LookupError("Bugün için bu kodla rezervasyon yok.")
    if r.status == ReservationStatus.CHECKED_IN.value:
        unit = (await requests_svc.unit_names(session, {r.unit_id})).get(r.unit_id, "")
        name = (await area_names(session, {r.area_id})).get(r.area_id, "")
        raise _conflict("already", f"Giriş zaten kaydedildi ({unit}, {name} {hm(r.start_time)}).")
    return await check_in(session, r, actor=actor, now=now)


# --- Onay mekanizması (ActionType.RESERVATION) ----------------------------------------------


async def _of_request(session: AsyncSession, request: ApprovalRequest) -> Reservation | None:
    if request.subject_id is None:  # pragma: no cover
        return None
    return await get(session, request.subject_id)


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    r = await _of_request(session, request)
    if r is None or r.status != ReservationStatus.PENDING.value:
        raise approvals_svc.ExecutionError("Rezervasyon artık onay beklemiyor.")
    now = request.decided_at or request.created_at
    try:
        await _approve(
            session, r, actor=f"Onay talebi #{request.number}", now=now,
            today=local_now(now).date(),
        )  # fmt: skip
    except OperationRuleError as exc:
        raise approvals_svc.ExecutionError(exc.message) from exc


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    """Onay talebi reddedildi ya da geri çekildi: rezervasyon reddedilir / iptal olur."""
    r = await _of_request(session, request)
    if r is None or r.status != ReservationStatus.PENDING.value:
        return
    now = request.decided_at or request.created_at
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        row = (
            await session.execute(
                select(ApprovalDecision.user_name, ApprovalDecision.comment)
                .where(
                    ApprovalDecision.request_id == request.id,
                    ApprovalDecision.revision == request.revision,
                    ApprovalDecision.decision == Decision.REJECT.value,
                )
                .order_by(ApprovalDecision.created_at.desc())
                .limit(1)
            )
        ).first()
        name, comment = (row[0], row[1]) if row else (None, None)
        await _reject(
            session, r, (comment or "Onay talebi reddedildi.").strip(),
            actor=name or f"Onay talebi #{request.number}", now=now,
        )  # fmt: skip
        return
    await cancel(
        session, r, "Onay talebi geri çekildi.", actor=request.requested_by_name,
        by_staff=False, now=now, today=local_now(now).date(),
    )  # fmt: skip


approvals_svc.register_executor(ActionType.RESERVATION, _execute)
approvals_svc.register_closer(ActionType.RESERVATION, _close)


# --- Listeler ve rapor ------------------------------------------------------------------------


def list_query(
    *, day: date | None, area_id: uuid.UUID | None, status: ReservationStatus | None
) -> Select[Reservation]:
    query = select(Reservation)
    if day is not None:
        query = query.where(Reservation.date == day)
    if area_id is not None:
        query = query.where(Reservation.area_id == area_id)
    if status is not None:
        query = query.where(Reservation.status == status.value)
    return query.order_by(Reservation.starts_at, Reservation.id)


def waitlist_query(*, day: date | None) -> Select[ReservationWaitlist]:
    query = select(ReservationWaitlist).where(
        ReservationWaitlist.status.in_([s.value for s in OPEN_WAIT])
    )
    if day is not None:
        query = query.where(ReservationWaitlist.date == day)
    return query.order_by(ReservationWaitlist.starts_at, ReservationWaitlist.created_at)


@dataclass(frozen=True, slots=True)
class ReportRow:
    area_id: uuid.UUID
    area_name: str
    total: int
    cancelled: int
    checked_in: int
    no_show: int
    no_show_rate: int | None
    occupancy: Decimal
    revenue: Decimal


async def report(session: AsyncSession, to: date) -> tuple[date, date, list[ReportRow]]:
    """Son 30 gün, alan bazında (REZ-03 kullanım raporu); sayım ve toplam veritabanında."""
    start = to - timedelta(days=REPORT_DAYS - 1)
    targets = await areas(session)
    rules = await rules_for(session, [a.id for a in targets])
    counts: dict[uuid.UUID, Counter[str]] = {a.id: Counter() for a in targets}
    for area_id, status, n in await session.execute(
        select(Reservation.area_id, Reservation.status, func.count())
        .where(Reservation.date.between(start, to))
        .group_by(Reservation.area_id, Reservation.status)
    ):
        if area_id in counts:
            counts[area_id][status] = n
    revenue_rows = await session.execute(
        select(Reservation.area_id, func.sum(Reservation.fee))
        .where(
            Reservation.date.between(start, to),
            Reservation.charge_entry_id.is_not(None),
            Reservation.refund_entry_id.is_(None),
        )
        .group_by(Reservation.area_id)
    )
    revenue = {row[0]: row[1] for row in revenue_rows.all()}
    rows: list[ReportRow] = []
    for a in targets:
        c, r = counts[a.id], rules[a.id]
        ended = sum(c[s.value] for s in (ReservationStatus.CHECKED_IN, ReservationStatus.COMPLETED,
                                         ReservationStatus.NO_SHOW))  # fmt: skip
        rows.append(
            ReportRow(
                area_id=a.id,
                area_name=a.name,
                total=sum(c.values()),
                cancelled=c[ReservationStatus.CANCELLED.value],
                checked_in=c[ReservationStatus.CHECKED_IN.value],
                no_show=c[ReservationStatus.NO_SHOW.value],
                no_show_rate=no_show_rate(r.verify, c[ReservationStatus.NO_SHOW.value], ended),
                occupancy=occupancy(
                    sum(c[s.value] for s in USED), len(slots(r)), r.capacity, REPORT_DAYS
                ),
                revenue=round_money(revenue.get(a.id) or ZERO),
            )
        )
    return start, to, rows


# --- Zamanlanmış iş (cli: run-reservation-jobs, 5 dakikada bir) -----------------------------


@dataclass(slots=True)
class JobResult:
    completed: int = 0
    no_show: int = 0
    restricted: int = 0
    expired_offers: int = 0
    new_offers: int = 0


async def run_jobs(session: AsyncSession, *, now: datetime) -> JobResult:
    """Biten onaylı rezervasyonu kapatır (doğrulama yoksa tamamlandı, varsa gelinmedi; kısıt
    eşiğine ulaşınca sakine bildirim) ve süresi dolan bekleme teklifini sıradakine geçirir."""
    result = JobResult()
    today = local_now(now).date()
    ended = list(
        await session.scalars(
            select(Reservation)
            .where(
                Reservation.status == ReservationStatus.APPROVED.value,
                Reservation.ends_at <= now,
            )
            .order_by(Reservation.ends_at)
            .with_for_update(skip_locked=True)
        )
    )
    rules = await rules_for(session, sorted({r.area_id for r in ended}))
    names = await area_names(session, {r.area_id for r in ended})
    for r in ended:
        status = close_status(rules[r.area_id].verify)
        r.status, r.closed_at = status.value, now
        if status is ReservationStatus.COMPLETED:
            result.completed += 1
            continue
        result.no_show += 1
        await session.flush()
        until = await noshow_until(session, rules[r.area_id], r.unit_id, r.area_id, today)
        if until is not None:
            result.restricted += 1
            await _notify(
                session, await _booker_users(session, r.person_id, r.unit_id, today),
                kind="reservation.noshow_blocked",
                title=f"Rezervasyon kısıtı: {names.get(r.area_id, '')}",
                body=blocked_message(names.get(r.area_id, ""), until), link_id=r.id,
                key=f"reservation:{r.id}:noshow_blocked",
            )  # fmt: skip
    await session.flush()
    stale = list(
        await session.scalars(
            select(ReservationWaitlist)
            .where(
                or_(
                    and_(
                        ReservationWaitlist.status == WaitlistStatus.OFFERED.value,
                        ReservationWaitlist.offered_until <= now,
                    ),
                    and_(
                        ReservationWaitlist.status.in_([s.value for s in OPEN_WAIT]),
                        ReservationWaitlist.starts_at <= now,
                    ),
                )
            )
            .order_by(ReservationWaitlist.offered_until)
        )
    )
    for entry in stale:
        await _lock_area(session, entry.area_id)
        await session.refresh(entry)
        if WaitlistStatus(entry.status) not in OPEN_WAIT:
            continue
        was_offered = entry.status == WaitlistStatus.OFFERED.value
        entry.status, entry.closed_at = WaitlistStatus.EXPIRED.value, now
        await session.flush()
        if was_offered:
            result.expired_offers += 1
            result.new_offers += len(
                await offer_next(session, entry.area_id, entry.starts_at, entry.ends_at, now=now)
            )
    return result


async def run_all_jobs(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[tuple[uuid.UUID, JobResult]]:
    """Site başına ayrı transaction — bir sitedeki hata diğerini durdurmaz."""
    with all_sites_scope():
        async with factory() as session:
            due = set(
                await session.scalars(
                    select(Reservation.site_id)
                    .where(
                        Reservation.status == ReservationStatus.APPROVED.value,
                        Reservation.ends_at <= now,
                    )
                    .distinct()
                )
            ) | set(
                await session.scalars(
                    select(ReservationWaitlist.site_id)
                    .where(
                        ReservationWaitlist.status.in_([s.value for s in OPEN_WAIT]),
                        or_(
                            ReservationWaitlist.offered_until <= now,
                            ReservationWaitlist.starts_at <= now,
                        ),
                    )
                    .distinct()
                )
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, JobResult]] = []
    for site_id in sorted(due):
        with site_scope(site_id):
            async with factory() as session, session.begin():
                results.append((site_id, await run_jobs(session, now=now)))
    return results
