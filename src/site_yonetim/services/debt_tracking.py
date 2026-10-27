"""Borç takibi: yaşlandırma, hatırlatma/arama/not kaydı, ihtarname gönderimi ve tebliğ, kademe
işi (FIN-12, servis isteği 29). Açık site kapsamında.

- Yaşlandırma özet bakiyeden (`account_balances`) — toplam, borçlu listesinin toplamına eşittir.
- **Gönderim K5'e bağlı:** SMS/bildirim sağlayıcısı seçilene kadar hatırlatma yalnız kayda
  geçer, mesaj gitmez (yanıt mesajı bunu söyler). Arama kademesinde yöneticiye bildirim
  merkezinden görev bildirimi gider.
- Gece işi: vadesi geçmiş her hesap için gelinen kademeyi aynı açık vade için **bir kez** yazar;
  yürürlükteki ödeme planı olan hesaba normal hatırlatma yazılmaz.
- İhtarname: "İhtarname" onay kuralı varsa kayıt onay bekler (202), onaylanınca `sent`.
- Borçlu bilgisi hiçbir sakin ucunda dönmez (bu servis yalnız yönetim uçlarından çağrılır).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.debt_tracking import (
    BUCKET_LABELS,
    ActionKind,
    AgingBucket,
    CollectionChannel,
    LetterStatus,
    PlanStatus,
    ReminderStage,
    bucket_of,
    check_action,
    check_letter,
    check_served,
    due_stages,
)
from site_yonetim.domain.money import EPSILON, ZERO
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.text import format_date_tr
from site_yonetim.models import (
    AccountBalance,
    Block,
    CollectionAction,
    DemandLetter,
    LedgerAccount,
    Site,
    Unit,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import finance_params, payment_plans
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import roles as roles_svc
from site_yonetim.services.approvals import ExecutionError, Person

SYSTEM_NAME = "Sistem (kademe işi)"
NOT_SENT = "Gönderim servisi (SMS/bildirim, K5) henüz yok; kayıt tutuldu, mesaj gitmedi."


# --- Yaşlandırma --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Aging:
    as_of: date
    buckets: list[tuple[AgingBucket, str, int, Decimal]]
    by_block: list[tuple[str, dict[AgingBucket, Decimal], Decimal]]


async def aging(session: AsyncSession, today: date) -> Aging:
    """Gecikme günü = bugün − en eski açık vade (docs/04 §13); kovalar veritabanında toplanır."""
    days = func.greatest(0, today - func.coalesce(AccountBalance.oldest_open_due_date, today))
    rows = await session.execute(
        select(Block.name, days.label("days"), func.count(), func.sum(AccountBalance.balance))
        .select_from(AccountBalance)
        .join(LedgerAccount, LedgerAccount.id == AccountBalance.account_id)
        .join(Unit, and_(Unit.id == LedgerAccount.unit_id, Unit.site_id == LedgerAccount.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(AccountBalance.balance > EPSILON)
        .group_by(Block.name, days)
    )
    counts: dict[AgingBucket, int] = defaultdict(int)
    sums: dict[AgingBucket, Decimal] = defaultdict(lambda: ZERO)
    blocks: dict[str, dict[AgingBucket, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for block, overdue, count, amount in rows:
        bucket = bucket_of(int(overdue))
        counts[bucket] += count
        sums[bucket] += amount
        blocks[block][bucket] += amount
    return Aging(
        today,
        [(b, BUCKET_LABELS[b], counts[b], sums[b]) for b in AgingBucket],
        [
            (name, {b: values[b] for b in AgingBucket}, sum(values.values(), ZERO))
            for name, values in sorted(blocks.items())
        ],
    )


# --- Takip durumu ve zaman çizelgesi ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FollowUp:
    plan_status: str | None
    letter_status: str | None
    last_action_at: datetime | None
    reminder_count: int


async def status_map(session: AsyncSession, today: date) -> dict[uuid.UUID, FollowUp]:
    """Kaydı olan hesaplar için takip özeti (borçlular listesinin "Takip" sütunu)."""
    actions = await session.execute(
        select(
            CollectionAction.account_id,
            func.max(CollectionAction.created_at),
            func.count().filter(CollectionAction.kind == ActionKind.REMINDER.value),
        ).group_by(CollectionAction.account_id)
    )
    letters = await session.execute(
        select(DemandLetter.account_id, DemandLetter.status, DemandLetter.created_at)
        .where(DemandLetter.status != LetterStatus.REJECTED.value)
        .order_by(DemandLetter.created_at)
    )
    out: dict[uuid.UUID, dict[str, object]] = defaultdict(
        lambda: {"plan": None, "letter": None, "last": None, "reminders": 0}
    )
    for account_id, last, reminders in actions:
        out[account_id].update(last=last, reminders=reminders)
    for account_id, status, created in letters:
        entry = out[account_id]
        entry["letter"] = status
        if entry["last"] is None or created > entry["last"]:  # type: ignore[operator]
            entry["last"] = created
    for plan, status in await payment_plans.plan_statuses(session, today):
        entry = out[plan.account_id]
        if entry["plan"] is None or status in (PlanStatus.ACTIVE, PlanStatus.BROKEN):
            entry["plan"] = status.value
    return {
        account_id: FollowUp(
            v["plan"],  # type: ignore[arg-type]
            v["letter"],  # type: ignore[arg-type]
            v["last"],  # type: ignore[arg-type]
            v["reminders"],  # type: ignore[arg-type]
        )
        for account_id, v in out.items()
    }


@dataclass(frozen=True, slots=True)
class TimelineItem:
    id: uuid.UUID
    kind: str  # reminder | call | note | demand_letter | payment_plan
    channel: str | None
    note: str | None
    by: str
    at: datetime
    letter: DemandLetter | None = None
    plan_id: uuid.UUID | None = None
    plan_status: str | None = None
    stage: str | None = None


async def timeline(session: AsyncSession, account_id: uuid.UUID, today: date) -> list[TimelineItem]:
    """Hesabın tüm takip kayıtları, yeniden eskiye (sayfalama uçta)."""
    items: list[TimelineItem] = [
        TimelineItem(a.id, a.kind, a.channel, a.note, a.by_name, a.created_at, stage=a.stage)
        for a in await session.scalars(
            select(CollectionAction).where(CollectionAction.account_id == account_id)
        )
    ]
    items += [
        TimelineItem(
            letter.id,
            "demand_letter",
            letter.method,
            letter.note,
            letter.created_by,
            letter.created_at,
            letter=letter,
        )
        for letter in await session.scalars(
            select(DemandLetter).where(DemandLetter.account_id == account_id)
        )
    ]
    for plan, status in await payment_plans.plan_statuses(session, today, account_id=account_id):
        items.append(
            TimelineItem(
                plan.id,
                "payment_plan",
                None,
                f"{plan.installment_count} taksit, toplam {plan.total:.2f}".replace(".", ","),
                plan.created_by,
                plan.created_at,
                plan_id=plan.id,
                plan_status=status.value,
            )
        )
    return sorted(items, key=lambda i: i.at, reverse=True)


# --- Elle kayıt ---------------------------------------------------------------------------


def action_message(kind: ActionKind, channel: CollectionChannel | None) -> str:
    if kind is ActionKind.REMINDER:
        via = "SMS" if channel is CollectionChannel.SMS else "uygulama bildirimi"
        return f"Hatırlatma ({via}) kaydedildi. {NOT_SENT}"
    return "Arama notu kaydedildi." if kind is ActionKind.CALL else "Not kaydedildi."


async def add_action(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    kind: str | None,
    channel: str | None,
    note: str | None,
    actor: Person,
) -> CollectionAction:
    picked, via, text = check_action(kind, channel, note)
    action = CollectionAction(
        id=uuid.uuid7(),
        account_id=account.id,
        kind=picked.value,
        channel=via.value if via else None,
        note=text,
        by_user_id=actor.user_id,
        by_name=actor.name,
    )
    session.add(action)
    await session.flush()
    return action


async def bulk_reminders(
    session: AsyncSession, account_ids: list[uuid.UUID], *, channel: str | None, actor: Person
) -> int:
    via = CollectionChannel.SMS if channel == CollectionChannel.SMS.value else CollectionChannel.APP
    found = list(
        await session.scalars(select(LedgerAccount.id).where(LedgerAccount.id.in_(account_ids)))
    )
    session.add_all(
        CollectionAction(
            account_id=account_id,
            kind=ActionKind.REMINDER.value,
            channel=via.value,
            note="Toplu hatırlatma",
            by_user_id=actor.user_id,
            by_name=actor.name,
        )
        for account_id in found
    )
    await session.flush()
    return len(found)


# --- İhtarname ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LetterResult:
    letter: DemandLetter
    request: ApprovalRequest | None


async def add_letter(
    session: AsyncSession,
    site: Site,
    account: LedgerAccount,
    *,
    method: str | None,
    sent_on: date | None,
    tracking_no: str | None,
    note: str | None,
    actor: Person,
    today: date,
    now: datetime,
) -> LetterResult:
    picked, day, tracking = check_letter(method, sent_on, tracking_no, today)
    needs_approval = (
        await approvals_svc.match_rule(
            session, ActionType.DEMAND_LETTER, amount=None, source=None, today=today
        )
        is not None
    )
    letter = DemandLetter(
        id=uuid.uuid7(),
        account_id=account.id,
        method=picked.value,
        sent_on=day,
        tracking_no=tracking,
        note=" ".join((note or "").split())[:500] or None,
        status=(LetterStatus.APPROVAL_PENDING if needs_approval else LetterStatus.SENT).value,
        created_by_id=actor.user_id,
        created_by=actor.name,
    )
    session.add(letter)
    await session.flush()
    request = None
    if needs_approval:
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.DEMAND_LETTER,
                title=f"İhtarname — {account.reference_code}, {format_date_tr(day)}",
                summary={
                    "reference_code": account.reference_code,
                    "method": picked.value,
                    "sent_on": day.isoformat(),
                    "tracking_no": tracking,
                },
                payload={"letter_id": str(letter.id)},
                subject_type="demand_letters",
                subject_id=letter.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        letter.approval_request_id = request.id
        await session.flush()
    return LetterResult(letter, request)


async def get_letter(session: AsyncSession, letter_id: uuid.UUID) -> DemandLetter | None:
    found: DemandLetter | None = await session.scalar(
        select(DemandLetter).where(DemandLetter.id == letter_id).with_for_update()
    )
    return found


class LetterStateError(Exception):
    """409: ihtarname bu işleme uygun durumda değil."""


async def mark_served(
    session: AsyncSession, letter: DemandLetter, served_on: date, today: date
) -> DemandLetter:
    if letter.status not in (LetterStatus.SENT.value, LetterStatus.SERVED.value):
        raise LetterStateError("Onay bekleyen ya da reddedilen ihtarnamede tebliğ girilemez.")
    check_served(served_on, letter.sent_on, today)
    letter.served_on = served_on
    letter.status = LetterStatus.SERVED.value
    await session.flush()
    return letter


async def _letter_of(session: AsyncSession, request: ApprovalRequest) -> DemandLetter | None:
    return await session.get(DemandLetter, uuid.UUID((request.payload or {})["letter_id"]))


async def _execute_letter(session: AsyncSession, request: ApprovalRequest) -> None:
    letter = await _letter_of(session, request)
    if letter is None or letter.status != LetterStatus.APPROVAL_PENDING.value:
        raise ExecutionError("İhtarname artık onay beklemiyor.")
    letter.status = LetterStatus.SENT.value
    await session.flush()


async def _close_letter(session: AsyncSession, request: ApprovalRequest) -> None:
    letter = await _letter_of(session, request)
    if letter is not None and letter.status == LetterStatus.APPROVAL_PENDING.value:
        letter.status = LetterStatus.REJECTED.value
        await session.flush()


approvals_svc.register_executor(ActionType.DEMAND_LETTER, _execute_letter)
approvals_svc.register_closer(ActionType.DEMAND_LETTER, _close_letter)


# --- Kademe işi ---------------------------------------------------------------------------

_STAGE_ACTION = {
    ReminderStage.APP: (
        ActionKind.REMINDER,
        CollectionChannel.APP,
        "Kademe 1 — uygulama bildirimi",
    ),
    ReminderStage.SMS: (ActionKind.REMINDER, CollectionChannel.SMS, "Kademe 2 — SMS hatırlatması"),
    ReminderStage.CALL: (
        ActionKind.CALL,
        CollectionChannel.PHONE,
        "Kademe 3 — yönetici araması (görev)",
    ),
    ReminderStage.DEMAND_LETTER: (ActionKind.NOTE, None, "İhtarname hazırlığı kademesi"),
}


async def run_stages(session: AsyncSession, site: Site, today: date) -> int:
    """Vadesi geçmiş hesaplara gelinen kademeyi bir kez yazar (aynı açık vade için). Arama ve
    ihtarname kademesinde finans yetkililerine bildirim merkezi üzerinden görev gider."""
    params = await finance_params.current(session, today)
    reminder_days: list[int] = params["reminder_days"]
    letter_day: int = params["demand_letter_day"]
    in_plan = {
        plan.account_id
        for plan, status in await payment_plans.plan_statuses(session, today)
        if status is PlanStatus.ACTIVE
    }
    rows = await session.execute(
        select(AccountBalance.account_id, AccountBalance.oldest_open_due_date).where(
            AccountBalance.balance > EPSILON,
            AccountBalance.oldest_open_due_date.is_not(None),
            AccountBalance.oldest_open_due_date < today,
        )
    )
    existing = {
        (a, d, s)
        for a, d, s in await session.execute(
            select(
                CollectionAction.account_id, CollectionAction.due_ref, CollectionAction.stage
            ).where(CollectionAction.stage.is_not(None))
        )
    }
    written = 0
    tasks: list[str] = []
    for account_id, due in rows:
        if account_id in in_plan or due is None:
            continue
        for stage in due_stages((today - due).days, reminder_days, letter_day):
            if (account_id, due, stage.value) in existing:
                continue
            kind, channel, note = _STAGE_ACTION[stage]
            session.add(
                CollectionAction(
                    account_id=account_id,
                    kind=kind.value,
                    channel=channel.value if channel else None,
                    note=note,
                    stage=stage.value,
                    due_ref=due,
                    by_name=SYSTEM_NAME,
                )
            )
            written += 1
            if stage in (ReminderStage.CALL, ReminderStage.DEMAND_LETTER):
                tasks.append(stage.value)
    await session.flush()
    if tasks:
        users = await roles_svc.permission_holders(session, site, Permission.FINANCE_PAYMENT_RECORD)
        await notify_svc.notify(
            session,
            users,
            notify_svc.Message(
                category=NotificationCategory.FINANCIAL,
                kind="collection.task",
                title=f"Borç takibi: {tasks.count('call')} arama, "
                f"{tasks.count('demand_letter')} ihtarname hazırlığı görevi",
                body="Borçlular › Borç takibi ekranında hesapları görün.",
                link_entity="debtors",
                link_id=None,
            ),
            dedupe_key=f"collection-task:{today.isoformat()}",
        )
    return written
