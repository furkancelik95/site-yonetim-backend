"""Demo verisi — genel kurul (servis istekleri 75–77): ekranlar boş görünmesin.

- Geçmiş olağan genel kurul (~5 ay önce): çağrı gönderilmiş ve çoğu okunmuş, hazirun (kendisi ve
  fiziki vekil), dört madde oylanmış (biri 4/5 tüm malik çoğunluğuna takılıp RET), tutanak
  kesinleşmiş, kabul edilen kararlar karar defterinde.
- Yaklaşan olağanüstü genel kurul (25 gün sonra): format ve karar türleri seçilmiş, çağrısı 3 gün
  önce maliklere gönderilmiş; okuma onaylarının bir kısmı gelmiş.
Hepsi gerçek servislerden geçer (nisap, vekâlet sınırı, değişmez kayıtlar aynı).
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.assembly import Ballot
from site_yonetim.domain.management import MeetingKind
from site_yonetim.domain.operations import Channel
from site_yonetim.models import AnnouncementDelivery, Meeting
from site_yonetim.services import assembly as assembly_svc
from site_yonetim.services import meetings as meetings_svc
from site_yonetim.services.charging import BUSINESS_TZ

MANAGER = "Selin Aydın"
PAST_AGENDA = (
    "Açılış, yoklama ve divan heyetinin seçimi",
    "Yönetim kurulu faaliyet ve denetim raporlarının görüşülmesi",
    "2026 işletme projesinin (bütçe) kabulü",
    "Otopark girişine bariyer sistemi kurulması (tüm maliklerin 4/5'i)",
)
PAST_DECISIONS = (
    "Divan başkanlığına Hasan Öztürk, yazmanlığa Elif Kara seçildi.",
    "Faaliyet ve denetim raporları okunarak yönetim kurulu ibra edildi.",
    "2026 işletme projesi aylık 9.500.000,00 TL'lik bütçeyle kabul edildi.",
    "",
)
UPCOMING_AGENDA = (
    "Açılış ve divan heyetinin seçimi",
    "Çatı yalıtımı için olağanüstü katılım payı toplanması",
    "Site güvenlik firması sözleşmesinin yenilenmesi",
)


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), BUSINESS_TZ)


async def _mark_some_read(
    session: AsyncSession, announcement_id: uuid.UUID, ratio_in_ten: int, at: datetime
) -> None:
    people = list(
        await session.scalars(
            select(AnnouncementDelivery.person_id)
            .where(
                AnnouncementDelivery.announcement_id == announcement_id,
                AnnouncementDelivery.channel == Channel.IN_APP.value,
            )
            .order_by(AnnouncementDelivery.person_id)
        )
    )
    chosen = [p for i, p in enumerate(people) if i % 10 < ratio_in_ten]
    if chosen:
        await session.execute(
            update(AnnouncementDelivery)
            .where(
                AnnouncementDelivery.announcement_id == announcement_id,
                AnnouncementDelivery.channel == Channel.IN_APP.value,
                AnnouncementDelivery.person_id.in_(chosen),
            )
            .values(read_at=at)
        )


def _choice(index: int, item: int) -> Ballot:
    """Kişi başına tutarlı, tekrarlanabilir oy dağılımı."""
    if item == 3:  # bariyer: çoğunluk kabul ama 4/5'e yetmez
        return (
            Ballot.FOR if index % 10 < 7 else (Ballot.AGAINST if index % 10 < 9 else Ballot.ABSTAIN)
        )
    if item == 2:
        return Ballot.FOR if index % 10 < 8 else Ballot.AGAINST
    return Ballot.FOR if index % 12 else Ballot.ABSTAIN


async def _past(session: AsyncSession, today: date) -> None:
    held_on = today - timedelta(days=150)
    sent_on = held_on - timedelta(days=20)
    meeting = await meetings_svc.create(
        session,
        kind=MeetingKind.GENERAL_ORDINARY.value,
        title=f"{held_on.year} olağan genel kurulu",
        scheduled_at=_at(held_on, 14),
        location="Sosyal tesis toplantı salonu",
        agenda=list(PAST_AGENDA),
        created_by=MANAGER,
    )
    items = (await meetings_svc.agenda(session, [meeting.id]))[meeting.id]
    await assembly_svc.prepare(
        session,
        meeting,
        today=sent_on,
        fmt="physical",
        item_types={str(items[3].id): "four_fifths"},
        second_call=False,
    )
    invited = await assembly_svc.send_invitation(
        session,
        meeting,
        title=f"{held_on.year} olağan genel kurul toplantı çağrısı",
        body=(
            f"Sayın malik,\n{held_on:%d.%m.%Y} günü saat 14:00'te sosyal tesis toplantı salonunda "
            "olağan genel kurul toplantısı yapılacaktır.\n\nGündem:\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(PAST_AGENDA, start=1))
            + "\n\nToplantı nisabı sağlanamazsa ikinci toplantı bir hafta sonra aynı saat ve "
            "yerde yapılacaktır."
        ),
        ack_hours=72,
        by=MANAGER,
        now=_at(sent_on, 10),
        today=sent_on,
    )
    assembly = invited.state.assembly
    assert assembly is not None  # noqa: S101  # nosec B101
    assert assembly.invitation_announcement_id is not None  # noqa: S101  # nosec B101
    await _mark_some_read(session, assembly.invitation_announcement_id, 8, _at(sent_on, 20))

    now = _at(held_on, 14)
    roster = invited.state.roster
    keys = list(dict.fromkeys(r.key for r in roster))
    attending_keys = set(keys[: int(len(keys) * 0.72)])
    proxies = ("Murat Şahin", "Gülay Toprak")
    proxied = 0
    for index, row in enumerate(r for r in roster if r.key in attending_keys):
        use_proxy = index % 9 == 4 and proxied < 4
        await assembly_svc.set_attendance(
            session,
            meeting,
            unit_id=row.unit_id,
            mode="proxy" if use_proxy else "self",
            proxy_name=proxies[proxied // 2] if use_proxy else None,
            by=MANAGER,
            now=now,
            today=held_on,
        )
        proxied += int(use_proxy)
    order = {key: i for i, key in enumerate(keys)}
    present = [r for r in roster if r.key in attending_keys]
    for number, item in enumerate(items):
        await assembly_svc.open_vote(session, meeting, item.id, by=MANAGER, now=now, today=held_on)
        await assembly_svc.record_ballots(
            session,
            meeting,
            item.id,
            ballots={str(r.unit_id): _choice(order[r.key], number).value for r in present},
            user_id=None,
            by=MANAGER,
            today=held_on,
        )
        await assembly_svc.close_vote(session, meeting, item.id, by=MANAGER, now=now, today=held_on)
    await assembly_svc.save_minutes(
        session,
        meeting,
        notes={
            str(items[1].id): "Denetim raporu okundu; soru gelmedi.",
            str(items[2].id): "Asansör bakım kalemi 2 malikin sorusu üzerine açıklandı.",
        },
        decisions={str(i.id): d for i, d in zip(items, PAST_DECISIONS, strict=True) if d},
        opening="Toplantı 14:10'da yönetici tarafından açıldı; hazirun cetveli imzalatıldı.",
        today=held_on,
    )
    await assembly_svc.finalize(session, meeting, by=MANAGER, now=_at(held_on, 17), today=held_on)


async def _upcoming(session: AsyncSession, today: date) -> None:
    meet_on = today + timedelta(days=25)
    sent_on = today - timedelta(days=3)
    meeting = await meetings_svc.create(
        session,
        kind=MeetingKind.GENERAL_EXTRAORDINARY.value,
        title="Olağanüstü genel kurul — çatı yalıtımı",
        scheduled_at=_at(meet_on, 19),
        location="Sosyal tesis toplantı salonu",
        agenda=list(UPCOMING_AGENDA),
        created_by=MANAGER,
    )
    items = (await meetings_svc.agenda(session, [meeting.id]))[meeting.id]
    await assembly_svc.prepare(
        session,
        meeting,
        today=sent_on,
        fmt="physical",
        item_types={str(items[1].id): "all_majority"},
        second_call=False,
    )
    sent = await assembly_svc.send_invitation(
        session,
        meeting,
        title="Olağanüstü genel kurul toplantı çağrısı",
        body=(
            f"Sayın malik,\n{meet_on:%d.%m.%Y} günü saat 19:00'da sosyal tesis toplantı salonunda "
            "olağanüstü genel kurul toplantısı yapılacaktır.\n\nGündem:\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(UPCOMING_AGENDA, start=1))
            + "\n\nToplantı nisabı sağlanamazsa ikinci toplantı bir hafta sonra aynı saat ve "
            "yerde yapılacaktır. Katılamayacak malikler yazılı vekâletname ile temsil edilebilir."
        ),
        ack_hours=72,
        by=MANAGER,
        now=_at(sent_on, 10),
        today=sent_on,
    )
    assembly = sent.state.assembly
    if assembly is not None and assembly.invitation_announcement_id is not None:
        await _mark_some_read(session, assembly.invitation_announcement_id, 6, _at(sent_on, 18))


async def seed_assemblies(
    factory: async_sessionmaker[AsyncSession], site_id: uuid.UUID, today: date
) -> None:
    with site_scope(site_id):
        async with factory() as session, session.begin():
            if await session.scalar(select(Meeting.id).limit(1)) is not None:
                return
            await _past(session, today)
            await _upcoming(session, today)
            await session.flush()
