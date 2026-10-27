"""Genel kurul — servis istekleri 75–77 (Yazılım Dokümanı TOP-01, TOP-03 salonda oylama, TOP-04).
Açık site kapsamında; transaction'ı çağıran yönetir.

- Genel kurul dosyası (`assemblies`) ilk yazmada açılır; okuma hiçbir şey yazmaz (malik listesi
  hazirun başlayana kadar canlı hesaplanır, ilk hazirun kaydında sabitlenir).
- Çağrı maliklere (`owners_only`) uygulama + e-posta + SMS duyurusu olarak gider; ispat raporu
  duyurunun teslim/okuma kayıtlarıdır (gerçek e-posta/SMS gönderimi sağlayıcı seçilince — K5).
- Oy olayları değişmez (`assembly_ballots`); geçerli oy bölümün son olayıdır.
- Kesinleştirme: mevcut toplantı kararları (`meetings.decide`) yazılır, tutanak anlık görüntüsü ve
  kabul edilen kararlar karar defterine (site bazında artan sıra no) değişmez kayıt olarak eklenir.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain import assembly as rules
from site_yonetim.domain.assembly import (
    AssemblyError,
    AssemblyFormat,
    AttendanceMode,
    Ballot,
    BallotChannel,
    DecisionType,
    PersonRule,
    RosterUnit,
    Threshold,
    VoteStatus,
    conflict,
)
from site_yonetim.domain.management import AgendaResult, MeetingKind, MeetingStatus
from site_yonetim.domain.operations import Audience, Channel, Importance, OperationRuleError
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import (
    AnnouncementDelivery,
    Assembly,
    AssemblyAttendance,
    AssemblyBallot,
    AssemblyItem,
    AssemblyMinutes,
    AssemblyRosterEntry,
    AssemblySettings,
    Block,
    DecisionBookEntry,
    Document,
    Meeting,
    MeetingAgendaItem,
    Person,
    Unit,
    UnitParty,
)
from site_yonetim.services import announcements
from site_yonetim.services import meetings as meetings_svc

BUSINESS_TZ = ZoneInfo("Europe/Istanbul")
_BOOK_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")

NOT_GENERAL = "Genel kurul işlemleri yalnız genel kurul toplantılarında."
FINALIZED = "Tutanak kesinleşti; sonuç ve oy sayıları değiştirilemez."


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def local_day(moment: datetime) -> date:
    return moment.astimezone(BUSINESS_TZ).date()


# --- Ayarlar --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    decision_types: tuple[DecisionType, ...]
    quorum: Threshold
    second_call_quorum: Threshold | None
    proxy_limit: int
    person_rule: PersonRule
    notice_days: int
    second_call_days: int
    read_ack_proof: bool
    remote_voting: bool
    digital_proxy: bool
    legal_basis_document_id: uuid.UUID | None
    updated_at: datetime | None = None
    updated_by: str | None = None

    def decision_type(self, code: str) -> DecisionType | None:
        return next((t for t in self.decision_types if t.code == code), None)

    @property
    def default_type(self) -> str:
        return self.decision_types[0].code

    @property
    def default_format(self) -> AssemblyFormat:
        # Doküman varsayılanı hibrit; uzaktan oy hukuki teyit + SMS gelene kadar kapalı (B18).
        return AssemblyFormat.HYBRID if self.remote_voting else AssemblyFormat.PHYSICAL


DEFAULT_SETTINGS = Settings(
    rules.DEFAULT_DECISION_TYPES,
    rules.DEFAULT_QUORUM,
    None,
    rules.PROXY_LIMIT_DEFAULT,
    PersonRule.OWNER_GROUP,
    rules.NOTICE_DAYS_DEFAULT,
    rules.SECOND_CALL_DAYS_DEFAULT,
    True,
    False,
    False,
    None,
)


async def load_settings(session: AsyncSession) -> Settings:
    row = await session.scalar(select(AssemblySettings))
    if row is None:
        return DEFAULT_SETTINGS
    return Settings(
        rules.decision_types_of(row.decision_types),
        rules.threshold_of(row.quorum, rules.DEFAULT_QUORUM) or rules.DEFAULT_QUORUM,
        rules.threshold_of(row.second_call_quorum, None),
        row.proxy_limit,
        PersonRule(row.person_rule),
        row.notice_days,
        row.second_call_days,
        row.read_ack_proof,
        row.remote_voting,
        row.digital_proxy,
        row.legal_basis_document_id,
        row.updated_at or row.created_at,
        row.updated_by_name,
    )


_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class SettingsInput:
    """`_UNSET` alan değişmez (kısmi güncelleme)."""

    decision_types: object = _UNSET
    quorum: object = _UNSET
    second_call_quorum: object = _UNSET
    proxy_limit: object = _UNSET
    person_rule: object = _UNSET
    notice_days: object = _UNSET
    second_call_days: object = _UNSET
    read_ack_proof: object = _UNSET
    remote_voting: object = _UNSET
    digital_proxy: object = _UNSET
    legal_basis_document_id: object = _UNSET


async def _codes_in_use(session: AsyncSession) -> set[str]:
    """Kesinleşmemiş genel kurullarda kullanılan karar türleri (kaldırılamaz)."""
    rows = await session.scalars(
        select(AssemblyItem.decision_type)
        .join(
            Assembly,
            and_(Assembly.id == AssemblyItem.assembly_id, Assembly.site_id == AssemblyItem.site_id),
        )
        .where(Assembly.minutes_finalized_at.is_(None))
        .distinct()
    )
    return set(rows)


def _bool(value: object, key: str, errors: dict[str, str]) -> bool | None:
    if not isinstance(value, bool):
        errors[key] = "Evet ya da hayır seçin."
        return None
    return value


async def save_settings(session: AsyncSession, data: SettingsInput, *, by: str) -> Settings:
    current = await load_settings(session)
    errors: dict[str, str] = {}
    types = current.decision_types
    if data.decision_types is not _UNSET:
        checked = rules.check_decision_types(data.decision_types, errors)
        if checked is not None:
            removed = await _codes_in_use(session) - {t.code for t in checked}
            if removed:
                errors["decision_types"] = (
                    "Kesinleşmemiş bir genel kurulda kullanılan karar türü kaldırılamaz: "
                    + ", ".join(sorted(removed))
                    + "."
                )
            types = checked
    quorum = current.quorum
    if data.quorum is not _UNSET:
        quorum = rules.check_threshold(data.quorum, "quorum", errors) or quorum
    second = current.second_call_quorum
    if data.second_call_quorum is not _UNSET:
        second = (
            None
            if data.second_call_quorum is None
            else rules.check_threshold(data.second_call_quorum, "second_call_quorum", errors)
        )
    ints: dict[str, int] = {}
    for key, low, high in (
        ("proxy_limit", rules.PROXY_LIMIT_MIN, rules.PROXY_LIMIT_MAX),
        ("notice_days", 0, rules.DAYS_MAX),
        ("second_call_days", 0, rules.DAYS_MAX),
    ):
        raw = getattr(data, key)
        if raw is _UNSET:
            ints[key] = getattr(current, key)
            continue
        value = rules.bounded_int(raw, low, high)
        if value is None:
            errors[key] = (
                f"Kişi başı vekâlet {low}–{high}."
                if key == "proxy_limit"
                else f"Gün sayısı {low}–{high}."
            )
        else:
            ints[key] = value
    person_rule = current.person_rule
    if data.person_rule is not _UNSET:
        if data.person_rule not in {r.value for r in PersonRule}:
            errors["person_rule"] = "Kişi sayım kuralını seçin."
        else:
            person_rule = PersonRule(str(data.person_rule))
    flags: dict[str, bool] = {}
    for key in ("read_ack_proof", "remote_voting", "digital_proxy"):
        raw = getattr(data, key)
        if raw is _UNSET:
            flags[key] = getattr(current, key)
        elif (value_b := _bool(raw, key, errors)) is not None:
            flags[key] = value_b
    basis = current.legal_basis_document_id
    if data.legal_basis_document_id is not _UNSET:
        if data.legal_basis_document_id is None:
            basis = None
        else:
            try:
                basis = uuid.UUID(str(data.legal_basis_document_id))
            except ValueError:
                basis = None
            found = (
                await session.scalar(select(Document.id).where(Document.id == basis))
                if basis
                else None
            )
            if found is None:
                errors["legal_basis_document_id"] = "Belge arşivinde böyle bir belge yok."
    if errors:
        raise AssemblyError(422, "validation_error", "", errors)
    enabling = [k for k in ("remote_voting", "digital_proxy") if flags[k]]
    if enabling and basis is None:
        raise AssemblyError(
            422,
            "validation_error",
            "",
            {
                "legal_basis_document_id": "Uzaktan oy ve dijital vekâlet hukuki dayanak belgesi "
                "(yönetim planı maddesi, kurul kararı ya da mevzuat) olmadan açılamaz."
            },
        )
    if enabling and not rules.SMS_VERIFICATION_AVAILABLE:
        raise conflict(
            "sms_verification_unavailable",
            "Uzaktan oy ve dijital vekâlet her oyda SMS koduyla doğrulama ister; SMS doğrulaması "
            "devreye alınınca açılabilir.",
        )
    if person_rule is not current.person_rule:
        frozen = await session.scalar(
            select(func.count())
            .select_from(Assembly)
            .where(Assembly.roster_frozen_at.is_not(None), Assembly.minutes_finalized_at.is_(None))
        )
        if frozen:
            raise conflict(
                "assembly_in_progress",
                "Hazirunu başlamış ve tutanağı kesinleşmemiş genel kurul varken kişi sayım kuralı "
                "değiştirilemez.",
            )
    row = await session.scalar(select(AssemblySettings).with_for_update())
    if row is None:
        row = AssemblySettings(id=uuid.uuid7())
        session.add(row)
    row.decision_types = [t.as_dict() for t in types]
    row.quorum = quorum.as_dict()
    row.second_call_quorum = second.as_dict() if second else None
    row.proxy_limit = ints["proxy_limit"]
    row.notice_days = ints["notice_days"]
    row.second_call_days = ints["second_call_days"]
    row.person_rule = person_rule.value
    row.read_ack_proof = flags["read_ack_proof"]
    row.remote_voting = flags["remote_voting"]
    row.digital_proxy = flags["digital_proxy"]
    row.legal_basis_document_id = basis
    row.updated_by_name = by
    await session.flush()
    await session.refresh(row)
    return await load_settings(session)


# --- Toplantı ve dosya ----------------------------------------------------------------------


async def general_meeting(
    session: AsyncSession, meeting_id: uuid.UUID, *, lock: bool = False
) -> Meeting | None:
    """Toplantı yoksa `None`; yönetim kurulu toplantısıysa 409 `not_general`."""
    meeting = await meetings_svc.get(session, meeting_id, lock=lock)
    if meeting is not None and meeting.kind == MeetingKind.BOARD.value:
        raise conflict("not_general", NOT_GENERAL)
    return meeting


async def find(
    session: AsyncSession, meeting_id: uuid.UUID, *, lock: bool = False
) -> Assembly | None:
    query = select(Assembly).where(Assembly.meeting_id == meeting_id)
    row: Assembly | None = await session.scalar(query.with_for_update() if lock else query)
    return row


async def _items(session: AsyncSession, assembly_id: uuid.UUID) -> list[AssemblyItem]:
    return list(
        await session.scalars(select(AssemblyItem).where(AssemblyItem.assembly_id == assembly_id))
    )


async def ensure(session: AsyncSession, meeting: Meeting, settings: Settings) -> Assembly:
    """Dosyayı kilitli getirir; yoksa açar (gündem maddeleri varsayılan karar türüyle)."""
    found = await find(session, meeting.id, lock=True)
    if found is not None:
        return found
    assembly = Assembly(id=uuid.uuid7(), meeting_id=meeting.id, format=settings.default_format)
    session.add(assembly)
    await session.flush()
    agenda = (await meetings_svc.agenda(session, [meeting.id]))[meeting.id]
    session.add_all(
        AssemblyItem(
            assembly_id=assembly.id, agenda_item_id=item.id, decision_type=settings.default_type
        )
        for item in agenda
    )
    await session.flush()
    return assembly


def _writable(meeting: Meeting, assembly: Assembly | None) -> None:
    if assembly is not None and assembly.minutes_finalized_at is not None:
        raise conflict("finalized", FINALIZED)
    if meeting.status != MeetingStatus.PLANNED.value:
        raise conflict(
            "invalid_status",
            "Genel kurul işlemleri yalnız planlanan (yapılmamış, iptal edilmemiş) toplantıda.",
        )


# --- Malik listesi ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RosterRow:
    unit_id: uuid.UUID
    unit_name: str
    block_name: str
    owner_ids: tuple[uuid.UUID, ...]
    owners: tuple[str, ...]
    key: str
    share_num: int
    share_den: int

    @property
    def unit(self) -> RosterUnit:
        return RosterUnit(self.unit_id, self.key, Fraction(self.share_num, self.share_den))


async def live_roster(session: AsyncSession, *, today: date, rule: PersonRule) -> list[RosterRow]:
    """Aktif ve bugün maliki olan bölümler (malik adları, arsa payı). Sıra: blok, numara."""
    rows = await session.execute(
        select(Unit, Block.name, Block.sort_order, Person.id, Person.first_name, Person.last_name)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .join(UnitParty, and_(UnitParty.unit_id == Unit.id, UnitParty.site_id == Unit.site_id))
        .join(Person, and_(Person.id == UnitParty.person_id, Person.site_id == UnitParty.site_id))
        .where(
            Unit.is_active.is_(True),
            UnitParty.role == PartyRole.OWNER.value,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    units: dict[uuid.UUID, tuple[Unit, str, int]] = {}
    owners: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    for unit, block_name, block_order, person_id, first, last in rows:
        units[unit.id] = (unit, block_name or "", block_order)
        owners.setdefault(unit.id, {})[person_id] = f"{first} {last}"
    out: list[RosterRow] = []
    for unit_id, (unit, block_name, _) in units.items():
        people = owners[unit_id]
        num, den = unit.land_share_numerator, unit.land_share_denominator
        out.append(
            RosterRow(
                unit_id,
                unit.display_name(block_name),
                block_name or "—",
                tuple(sorted(people)),
                tuple(sorted(people.values(), key=tr_lower)),
                rules.person_key(unit_id, people, rule),
                num if num is not None and den else 0,
                den if num is not None and den else 1,
            )
        )
    order = {
        uid: (units[uid][2], units[uid][1], len(units[uid][0].number), units[uid][0].number)
        for uid in units
    }
    out.sort(key=lambda r: order[r.unit_id])
    return out


async def frozen_roster(session: AsyncSession, assembly_id: uuid.UUID) -> list[RosterRow]:
    rows = await session.scalars(
        select(AssemblyRosterEntry)
        .where(AssemblyRosterEntry.assembly_id == assembly_id)
        .order_by(AssemblyRosterEntry.sort_order)
    )
    return [
        RosterRow(
            r.unit_id,
            r.unit_name,
            r.block_name,
            tuple(r.owner_person_ids),
            tuple(r.owner_names),
            r.person_key,
            r.share_numerator,
            r.share_denominator,
        )
        for r in rows
    ]


async def roster_of(
    session: AsyncSession, assembly: Assembly | None, *, today: date, settings: Settings
) -> list[RosterRow]:
    if assembly is not None and assembly.roster_frozen_at is not None:
        return await frozen_roster(session, assembly.id)
    return await live_roster(session, today=today, rule=settings.person_rule)


async def _freeze(
    session: AsyncSession, assembly: Assembly, *, today: date, now: datetime, settings: Settings
) -> list[RosterRow]:
    if assembly.roster_frozen_at is not None:
        return await frozen_roster(session, assembly.id)
    roster = await live_roster(session, today=today, rule=settings.person_rule)
    session.add_all(
        AssemblyRosterEntry(
            assembly_id=assembly.id,
            unit_id=r.unit_id,
            unit_name=r.unit_name,
            block_name=r.block_name,
            owner_person_ids=list(r.owner_ids),
            owner_names=list(r.owners),
            person_key=r.key,
            share_numerator=r.share_num,
            share_denominator=r.share_den,
            sort_order=index,
        )
        for index, r in enumerate(roster)
    )
    assembly.roster_frozen_at = now
    await session.flush()
    return roster


# --- Durum ---------------------------------------------------------------------------------


@dataclass(slots=True)
class State:
    meeting: Meeting
    agenda: list[MeetingAgendaItem]
    assembly: Assembly | None
    settings: Settings
    roster: list[RosterRow]
    attendance: list[AssemblyAttendance]
    items: dict[uuid.UUID, AssemblyItem]  # gündem maddesi kimliği → madde
    ballots: dict[uuid.UUID, dict[uuid.UUID, Ballot]]  # gündem maddesi → bölüm → oy
    minutes: AssemblyMinutes | None = None
    invitation_total: int = 0
    invitation_read: int = 0
    invitation_read_in_time: int = 0
    signed_document: Document | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> set[uuid.UUID]:
        return {a.unit_id for a in self.attendance}

    @property
    def roster_units(self) -> list[RosterUnit]:
        return [r.unit for r in self.roster]

    def item_type(self, agenda_item_id: uuid.UUID) -> str:
        item = self.items.get(agenda_item_id)
        return item.decision_type if item else self.settings.default_type

    def quorum(self) -> rules.QuorumResult:
        return rules.quorum(
            self.roster_units,
            self.present,
            self.settings.quorum,
            second_call=bool(self.assembly and self.assembly.second_call),
            second_call_rule=self.settings.second_call_quorum,
        )

    def any_open(self) -> bool:
        return any(i.status == VoteStatus.OPEN.value for i in self.items.values())

    def started(self) -> bool:
        return bool(self.attendance) or any(
            i.status != VoteStatus.NONE.value for i in self.items.values()
        )


async def current_ballots(
    session: AsyncSession, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[uuid.UUID, Ballot]]:
    """Madde kimliği (assembly_items.id) → bölüm → geçerli oy (son olay; NULL = silinmiş)."""
    out: dict[uuid.UUID, dict[uuid.UUID, Ballot]] = {i: {} for i in item_ids}
    if not item_ids:
        return out
    rows = await session.execute(
        select(AssemblyBallot.assembly_item_id, AssemblyBallot.unit_id, AssemblyBallot.choice)
        .where(AssemblyBallot.assembly_item_id.in_(list(item_ids)))
        .ext(distinct_on(AssemblyBallot.assembly_item_id, AssemblyBallot.unit_id))
        .order_by(AssemblyBallot.assembly_item_id, AssemblyBallot.unit_id, AssemblyBallot.id.desc())
    )
    for item_id, unit_id, choice in rows:
        if choice is not None:
            out[item_id][unit_id] = Ballot(choice)
    return out


async def load_state(
    session: AsyncSession,
    meeting: Meeting,
    *,
    today: date,
    assembly: Assembly | None = None,
    settings: Settings | None = None,
) -> State:
    settings = settings or await load_settings(session)
    assembly = assembly or await find(session, meeting.id)
    agenda = (await meetings_svc.agenda(session, [meeting.id]))[meeting.id]
    roster = await roster_of(session, assembly, today=today, settings=settings)
    if assembly is None:
        return State(meeting, agenda, None, settings, roster, [], {}, {})
    attendance = list(
        await session.scalars(
            select(AssemblyAttendance).where(AssemblyAttendance.assembly_id == assembly.id)
        )
    )
    items = {i.agenda_item_id: i for i in await _items(session, assembly.id)}
    by_id = await current_ballots(session, [i.id for i in items.values()])
    ballots = {aid: by_id[i.id] for aid, i in items.items()}
    state = State(meeting, agenda, assembly, settings, roster, attendance, items, ballots)
    state.minutes = await session.scalar(
        select(AssemblyMinutes).where(AssemblyMinutes.assembly_id == assembly.id)
    )
    if assembly.invitation_announcement_id is not None:
        deadline = _ack_deadline(assembly)
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count(AnnouncementDelivery.read_at),
                    func.count(AnnouncementDelivery.read_at).filter(
                        AnnouncementDelivery.read_at <= deadline
                    ),
                ).where(
                    AnnouncementDelivery.announcement_id == assembly.invitation_announcement_id,
                    AnnouncementDelivery.channel == Channel.IN_APP.value,
                )
            )
        ).one()
        state.invitation_total, state.invitation_read, state.invitation_read_in_time = row
    if assembly.signed_document_id is not None:
        state.signed_document = await session.get(Document, assembly.signed_document_id)
    return state


def _ack_deadline(assembly: Assembly) -> datetime:
    assert assembly.invitation_sent_at is not None  # noqa: S101  # nosec B101 — çağıran denetler
    return assembly.invitation_sent_at + timedelta(hours=assembly.invitation_ack_hours or 0)


def ack_deadline(assembly: Assembly) -> datetime | None:
    return _ack_deadline(assembly) if assembly.invitation_sent_at is not None else None


# --- 75 Hazırlık ve çağrı -------------------------------------------------------------------


async def prepare(
    session: AsyncSession,
    meeting: Meeting,
    *,
    today: date,
    fmt: str | None,
    item_types: Mapping[str, str] | None,
    second_call: bool | None,
) -> State:
    settings = await load_settings(session)
    existing = await find(session, meeting.id, lock=True)
    _writable(meeting, existing)
    state = await load_state(session, meeting, today=today, assembly=existing, settings=settings)
    errors: dict[str, str] = {}
    if fmt is not None and fmt not in {f.value for f in AssemblyFormat}:
        errors["format"] = "Format seçin."
    agenda_ids = {str(i.id): i.id for i in state.agenda}
    codes = {t.code for t in settings.decision_types}
    types: dict[uuid.UUID, str] = {}
    for key, code in (item_types or {}).items():
        if key not in agenda_ids:
            errors[f"item_types.{key}"] = "Gündem maddesi bu toplantıda yok."
        elif code not in codes:
            errors[f"item_types.{key}"] = "Karar türü seçin."
        else:
            types[agenda_ids[key]] = code
    if errors:
        raise AssemblyError(422, "validation_error", "", errors)
    assembly = existing or await ensure(session, meeting, settings)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    changes_setup = (fmt is not None and fmt != assembly.format) or any(
        state.items[aid].decision_type != code for aid, code in types.items()
    )
    if changes_setup and state.started():
        raise conflict("started", "Hazirun başladıktan sonra format ve karar türü değiştirilemez.")
    if (
        second_call is not None
        and second_call != assembly.second_call
        and any(i.status != VoteStatus.NONE.value for i in state.items.values())
    ):
        raise conflict("started", "Oylama başladıktan sonra ikinci toplantı işareti değişmez.")
    if fmt is not None and fmt != assembly.format:
        assembly.format = fmt
        assembly.legal_ack_by_name = None  # format değişince uyarı onayı sıfırlanır
        assembly.legal_ack_at = None
    for aid, code in types.items():
        state.items[aid].decision_type = code
    if second_call is not None:
        assembly.second_call = second_call
    await session.flush()
    return await load_state(session, meeting, today=today, assembly=assembly, settings=settings)


async def legal_ack(
    session: AsyncSession, meeting: Meeting, *, today: date, by: str, now: datetime
) -> State:
    settings = await load_settings(session)
    existing = await find(session, meeting.id, lock=True)
    _writable(meeting, existing)
    assembly = existing or await ensure(session, meeting, settings)
    assembly.legal_ack_by_name = by
    assembly.legal_ack_at = now
    await session.flush()
    return await load_state(session, meeting, today=today, assembly=assembly, settings=settings)


@dataclass(frozen=True, slots=True)
class InvitationSent:
    state: State
    recipients: int
    notice_short: bool
    last_day: date


async def send_invitation(
    session: AsyncSession,
    meeting: Meeting,
    *,
    title: str | None,
    body: str | None,
    ack_hours: int | None,
    by: str,
    now: datetime,
    today: date,
) -> InvitationSent:
    settings = await load_settings(session)
    existing = await find(session, meeting.id, lock=True)
    _writable(meeting, existing)
    fmt = existing.format if existing else settings.default_format
    if fmt != AssemblyFormat.PHYSICAL.value and not (existing and existing.legal_ack_at):
        raise conflict(
            "legal_ack_required",
            "Hibrit / ön oylama formatında önce hukuki geçerlilik uyarısını onaylayın.",
        )
    if existing is not None and existing.invitation_sent_at is not None:
        raise conflict("already_sent", "Bu toplantının çağrısı zaten gönderildi.")
    if meeting.scheduled_at <= now:
        raise conflict("meeting_past", "Tarihi geçmiş toplantıya çağrı gönderilemez.")
    errors: dict[str, str] = {}
    name = _clean(title)
    if not rules.INVITATION_TITLE_MIN <= len(name) <= rules.INVITATION_TITLE_MAX:
        errors["title"] = "Çağrıya bir başlık verin (3–120 karakter)."
    content = (body or "").strip()
    if not rules.INVITATION_BODY_MIN <= len(content) <= rules.INVITATION_BODY_MAX:
        errors["body"] = "Çağrı metnini yazın (tarih, yer, gündem)."
    hours = rules.ACK_HOURS_DEFAULT if ack_hours is None else ack_hours
    if not rules.ACK_HOURS_MIN <= hours <= rules.ACK_HOURS_MAX:
        errors["ack_hours"] = "Okuma onayı süresi 1–720 saat."
    if errors:
        raise AssemblyError(422, "validation_error", "", errors)
    assembly = existing or await ensure(session, meeting, settings)
    meeting_on = local_day(meeting.scheduled_at)
    try:
        published = await announcements.publish(
            session,
            announcements.NewAnnouncement(
                title=name,
                body=content,
                importance=Importance.IMPORTANT,
                audience=Audience.OWNERS_ONLY,
                block_ids=(),
                channels=(Channel.IN_APP, Channel.EMAIL, Channel.SMS),
                expires_on=meeting_on,
                is_pinned=True,
            ),
            published_by=by,
            now=now,
            today=today,
        )
    except OperationRuleError as exc:  # pragma: no cover - geçmiş tarih yukarıda engellendi
        raise conflict(exc.code, exc.message) from exc
    assembly.invitation_announcement_id = published.announcement.id
    assembly.invitation_sent_at = now
    assembly.invitation_by_name = by
    assembly.invitation_ack_hours = hours
    await session.flush()
    last_day = rules.notice_last_day(meeting_on, settings.notice_days)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    return InvitationSent(state, published.recipient_count, today > last_day, last_day)


@dataclass(frozen=True, slots=True)
class ProofRow:
    person_id: uuid.UUID
    name: str
    units: list[str]
    channels: list[str]
    sent_at: datetime | None
    read_at: datetime | None
    read_in_time: bool


def proof_query(announcement_id: uuid.UUID) -> Select[uuid.UUID, str, str]:
    return (
        select(Person.id, Person.first_name, Person.last_name)
        .join(
            AnnouncementDelivery,
            and_(
                AnnouncementDelivery.person_id == Person.id,
                AnnouncementDelivery.site_id == Person.site_id,
            ),
        )
        .where(
            AnnouncementDelivery.announcement_id == announcement_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
        )
        .order_by(Person.search_name, Person.id)
    )


async def proof_rows(
    session: AsyncSession,
    assembly: Assembly,
    people: Sequence[tuple[uuid.UUID, str, str]],
    *,
    today: date,
) -> list[ProofRow]:
    ids = [p[0] for p in people]
    if not ids or assembly.invitation_announcement_id is None:
        return []
    deliveries = await session.execute(
        select(
            AnnouncementDelivery.person_id,
            AnnouncementDelivery.channel,
            AnnouncementDelivery.sent_at,
            AnnouncementDelivery.read_at,
        ).where(
            AnnouncementDelivery.announcement_id == assembly.invitation_announcement_id,
            AnnouncementDelivery.person_id.in_(ids),
        )
    )
    channels: dict[uuid.UUID, list[str]] = {}
    sent: dict[uuid.UUID, datetime | None] = {}
    read: dict[uuid.UUID, datetime | None] = {}
    for person_id, channel, sent_at, read_at in deliveries:
        channels.setdefault(person_id, []).append(channel)
        if channel == Channel.IN_APP.value:
            sent[person_id], read[person_id] = sent_at, read_at
    unit_rows = await session.execute(
        select(UnitParty.person_id, Unit.number, Block.name)
        .join(Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(
            UnitParty.person_id.in_(ids),
            UnitParty.role == PartyRole.OWNER.value,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    units: dict[uuid.UUID, list[str]] = {}
    for person_id, number, block_name in unit_rows:
        units.setdefault(person_id, []).append(f"{block_name}-{number}" if block_name else number)
    deadline = _ack_deadline(assembly)
    order = [c.value for c in Channel]
    return [
        ProofRow(
            pid,
            f"{first} {last}",
            sorted(units.get(pid, [])),
            sorted(channels.get(pid, []), key=order.index),
            sent.get(pid),
            read.get(pid),
            read.get(pid) is not None and read[pid] <= deadline,  # type: ignore[operator]
        )
        for pid, first, last in people
    ]


# --- 76 Hazirun, vekâlet, oylama ---------------------------------------------------------------


async def _for_floor(
    session: AsyncSession, meeting: Meeting, *, today: date
) -> tuple[Assembly, Settings]:
    settings = await load_settings(session)
    existing = await find(session, meeting.id, lock=True)
    _writable(meeting, existing)
    return existing or await ensure(session, meeting, settings), settings


async def set_attendance(
    session: AsyncSession,
    meeting: Meeting,
    *,
    unit_id: uuid.UUID,
    mode: str | None,
    proxy_name: str | None,
    by: str,
    now: datetime,
    today: date,
) -> tuple[State, str]:
    assembly, settings = await _for_floor(session, meeting, today=today)
    if mode is not None and mode not in {m.value for m in AttendanceMode}:
        raise AssemblyError(422, "validation_error", "", {"mode": "Kendisi ya da vekil seçin."})
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    if state.any_open():
        raise conflict("voting_open", "Açık oylama varken hazirun değiştirilemez.")
    roster = await _freeze(session, assembly, today=today, now=now, settings=settings)
    entry = next((r for r in roster if r.unit_id == unit_id), None)
    if entry is None:
        raise AssemblyError(404, "not_found", "Bölüm malik listesinde yok.")
    row = await session.scalar(
        select(AssemblyAttendance).where(
            AssemblyAttendance.assembly_id == assembly.id, AssemblyAttendance.unit_id == unit_id
        )
    )
    if mode is None:
        if row is not None:
            await session.delete(row)
        await session.flush()
        state = await load_state(
            session, meeting, today=today, assembly=assembly, settings=settings
        )
        return state, f"{entry.unit_name} hazirundan çıkarıldı."
    name: str | None = None
    key: str | None = None
    if mode == AttendanceMode.PROXY.value:
        name = _clean(proxy_name)
        if not rules.PROXY_NAME_MIN <= len(name) <= rules.PROXY_NAME_MAX:
            raise AssemblyError(
                422, "validation_error", "", {"proxy_name": "Vekilin adını ve soyadını yazın."}
            )
        key = rules.proxy_name_key(name)
        carried = set(
            await session.scalars(
                select(AssemblyAttendance.unit_id).where(
                    AssemblyAttendance.assembly_id == assembly.id,
                    AssemblyAttendance.proxy_key == key,
                    AssemblyAttendance.unit_id != unit_id,
                )
            )
        )
        load = rules.proxy_load([r.unit for r in roster], carried | {unit_id})
        if load > settings.proxy_limit:
            raise conflict(
                "proxy_limit",
                f"Vekâlet sınırı aşılıyor: bir kişi en fazla {settings.proxy_limit} malikin vekili "
                "olabilir.",
            )
    if row is None:
        row = AssemblyAttendance(assembly_id=assembly.id, unit_id=unit_id)
        session.add(row)
    row.mode = mode
    row.proxy_name = name
    row.proxy_key = key
    row.recorded_by_name = by
    await session.flush()
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    suffix = f" (vekil: {name})" if name else ""
    return state, f"{entry.unit_name} hazirune eklendi{suffix}."


async def _item(state: State, agenda_item_id: uuid.UUID) -> AssemblyItem:
    item = state.items.get(agenda_item_id)
    if item is None:
        raise AssemblyError(404, "not_found", "Gündem maddesi bulunamadı.")
    return item


def result_snapshot(state: State, agenda_item_id: uuid.UUID, dtype: DecisionType) -> dict[str, Any]:
    """Kapanış sonucu (değişmez): sayılar, oranlar (malik listesinin toplam payına göre kesir
    metni) ve uygulanan karar türü."""
    t = rules.tally(state.roster_units, state.present, state.ballots[agenda_item_id], dtype)
    total = rules.total_share(state.roster_units) or Fraction(1)

    def frac(v: Fraction) -> str:
        return str(v / total)

    return {
        "type": dtype.as_dict(),
        "persons": {
            "for": t.persons.for_,
            "against": t.persons.against,
            "abstain": t.persons.abstain,
            "base": t.persons.base,
        },
        "shares": {
            "for": frac(t.shares.for_),
            "against": frac(t.shares.against),
            "abstain": frac(t.shares.abstain),
            "base": frac(t.shares.base),
        },
        "person_ok": t.person_ok,
        "share_ok": t.share_ok,
        "accepted": t.accepted,
    }


async def open_vote(
    session: AsyncSession,
    meeting: Meeting,
    agenda_item_id: uuid.UUID,
    *,
    by: str,
    now: datetime,
    today: date,
) -> State:
    assembly, settings = await _for_floor(session, meeting, today=today)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    item = await _item(state, agenda_item_id)
    if item.status != VoteStatus.NONE.value:
        raise conflict("invalid_status", "Bu maddenin oylaması zaten açıldı.")
    if state.any_open():
        raise conflict("voting_open", "Önce açık oylamayı kapatın.")
    if settings.decision_type(item.decision_type) is None:
        raise conflict(
            "unknown_decision_type",
            "Maddenin karar türü ayarlarda yok; Hazırlık sekmesinden karar türünü seçin.",
        )
    q = state.quorum()
    if not q.ok:
        raise conflict("no_quorum", rules.no_quorum_message(q))
    item.status = VoteStatus.OPEN.value
    item.opened_at = now
    item.opened_by_name = by
    await session.flush()
    return await load_state(session, meeting, today=today, assembly=assembly, settings=settings)


async def close_vote(
    session: AsyncSession,
    meeting: Meeting,
    agenda_item_id: uuid.UUID,
    *,
    by: str,
    now: datetime,
    today: date,
) -> tuple[State, bool]:
    assembly, settings = await _for_floor(session, meeting, today=today)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    item = await _item(state, agenda_item_id)
    if item.status != VoteStatus.OPEN.value:
        raise conflict("invalid_status", "Bu maddede açık oylama yok.")
    missing = rules.missing_ballots(state.present, state.ballots[agenda_item_id])
    if missing:
        raise AssemblyError(422, "missing_ballots", f"{missing} katılımcının oyu girilmedi.")
    dtype = settings.decision_type(item.decision_type)
    if dtype is None:  # pragma: no cover - açılışta denetlendi; ayar kaldırma engelli
        raise conflict("unknown_decision_type", "Maddenin karar türü ayarlarda yok.")
    item.result = result_snapshot(state, agenda_item_id, dtype)
    item.status = VoteStatus.CLOSED.value
    item.closed_at = now
    item.closed_by_name = by
    await session.flush()
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    return state, bool(item.result["accepted"])


async def record_ballots(
    session: AsyncSession,
    meeting: Meeting,
    agenda_item_id: uuid.UUID,
    *,
    ballots: Mapping[str, str | None],
    user_id: uuid.UUID | None,
    by: str,
    today: date,
) -> State:
    assembly, settings = await _for_floor(session, meeting, today=today)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    item = await _item(state, agenda_item_id)
    if item.status != VoteStatus.OPEN.value:
        raise conflict("invalid_status", "Oylama açık değil.")
    present = state.present
    errors: dict[str, str] = {}
    changes: dict[uuid.UUID, Ballot | None] = {}
    for raw_id, raw in ballots.items():
        key = f"ballots.{raw_id}"
        try:
            uid = uuid.UUID(raw_id)
        except ValueError:
            errors[key] = "Bölüm bulunamadı."
            continue
        if uid not in present:
            errors[key] = "Hazirunda olmayan bölüm oy kullanamaz."
        elif raw is not None and raw not in {b.value for b in Ballot}:
            errors[key] = "Kabul, ret ya da çekimser seçin."
        else:
            changes[uid] = Ballot(raw) if raw is not None else None
    if errors:
        raise AssemblyError(422, "validation_error", "", errors)
    current = state.ballots[agenda_item_id]
    after = dict(current)
    for uid, choice in changes.items():
        if choice is None:
            after.pop(uid, None)
        else:
            after[uid] = choice
    clash = rules.owner_conflicts(state.roster_units, after)
    if clash:
        names = next(", ".join(r.owners) for r in state.roster if r.key == clash[0])
        raise AssemblyError(
            422, "owner_conflict", f"{names} birden çok bölümünde farklı oy kullanamaz."
        )
    session.add_all(
        AssemblyBallot(
            assembly_item_id=item.id,
            unit_id=uid,
            choice=choice.value if choice else None,
            channel=BallotChannel.HALL.value,
            entered_by_user_id=user_id,
            entered_by_name=by,
        )
        for uid, choice in changes.items()
        if current.get(uid) is not choice
    )
    await session.flush()
    return await load_state(session, meeting, today=today, assembly=assembly, settings=settings)


# --- 77 Tutanak ve karar defteri ----------------------------------------------------------------


async def save_minutes(
    session: AsyncSession,
    meeting: Meeting,
    *,
    notes: Mapping[str, str] | None,
    decisions: Mapping[str, str] | None,
    opening: str | None,
    today: date,
) -> State:
    assembly, settings = await _for_floor(session, meeting, today=today)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    ids = {str(a): a for a in state.items}
    errors: dict[str, str] = {}
    for prefix, values, limit in (
        ("notes", notes or {}, rules.NOTE_MAX),
        ("decisions", decisions or {}, rules.DECISION_MAX),
    ):
        for key, value in values.items():
            if key not in ids:
                errors[f"{prefix}.{key}"] = "Gündem maddesi bu toplantıda yok."
            elif len((value or "").strip()) > limit:
                errors[f"{prefix}.{key}"] = f"En fazla {limit} karakter."
    if opening is not None and len(opening.strip()) > rules.OPENING_MAX:
        errors["opening"] = f"En fazla {rules.OPENING_MAX} karakter."
    if errors:
        raise AssemblyError(422, "validation_error", "", errors)
    for key, value in (notes or {}).items():
        state.items[ids[key]].minutes_note = (value or "").strip() or None
    for key, value in (decisions or {}).items():
        state.items[ids[key]].decision_text = _clean(value) or None
    if opening is not None:
        assembly.minutes_opening = opening.strip() or None
    await session.flush()
    return await load_state(session, meeting, today=today, assembly=assembly, settings=settings)


def attendance_sentence(state: State) -> str:
    q = state.quorum()
    proxies = sum(1 for a in state.attendance if a.mode == AttendanceMode.PROXY.value)
    text_ = (
        f"{q.persons_total} malikten {q.persons_present}'i "
        f"(arsa payının {rules.pct_tr(q.share_present, q.share_total)}'i) katıldı"
    )
    if proxies:
        text_ += f"; {proxies} bölüm vekille temsil edildi"
    if not q.required:
        text_ += ". Toplantı nisabı aranmadı (ikinci toplantı)."
    else:
        text_ += f". Toplantı nisabı {'sağlandı' if q.ok else 'sağlanamadı'}."
    return text_


def default_decision(accepted: bool) -> str:
    return "Kabul edildi." if accepted else "Reddedildi."


@dataclass(frozen=True, slots=True)
class Finalized:
    state: State
    book_numbers: list[int]


async def finalize(
    session: AsyncSession, meeting: Meeting, *, by: str, now: datetime, today: date
) -> Finalized:
    assembly, settings = await _for_floor(session, meeting, today=today)
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    open_items = [a for a in state.agenda if state.items[a.id].status != VoteStatus.CLOSED.value]
    if open_items:
        raise AssemblyError(
            422,
            "items_open",
            "Oylaması kapanmamış madde: "
            + ", ".join(f"{a.order}. {a.title}" for a in open_items)
            + ".",
        )
    note = attendance_sentence(state)
    results: list[tuple[MeetingAgendaItem, AssemblyItem, dict[str, Any], str]] = []
    for agenda_item in state.agenda:
        item = state.items[agenda_item.id]
        result = item.result or {}
        decision = item.decision_text or default_decision(bool(result.get("accepted")))
        results.append((agenda_item, item, result, decision))
    await meetings_svc.decide(
        session,
        meeting,
        attendance_note=note[:500],
        decisions=[
            meetings_svc.Decision(
                agenda_item.id,
                AgendaResult.ACCEPTED.value if result["accepted"] else AgendaResult.REJECTED.value,
                decision,
                result["persons"]["for"],
                result["persons"]["against"],
                result["persons"]["abstain"],
            )
            for agenda_item, _, result, decision in results
        ],
        now=now,
    )
    q = state.quorum()
    session.add(
        AssemblyMinutes(
            assembly_id=assembly.id,
            finalized_by_name=by,
            content={
                "meeting": {
                    "number": meeting.number,
                    "kind": meeting.kind,
                    "title": meeting.title,
                    "scheduled_at": meeting.scheduled_at.isoformat(),
                    "location": meeting.location,
                },
                "format": assembly.format,
                "second_call": assembly.second_call,
                "attendance": note,
                "quorum": {
                    "persons_present": q.persons_present,
                    "persons_total": q.persons_total,
                    "share_present": str(q.share_present / (q.share_total or 1)),
                    "required": q.required,
                    "ok": q.ok,
                },
                "proxies": sum(1 for a in state.attendance if a.mode == AttendanceMode.PROXY.value),
                "opening": assembly.minutes_opening,
                "items": [
                    {
                        "agenda_item_id": str(a.id),
                        "order": a.order,
                        "title": a.title,
                        "result": r,
                        "note": i.minutes_note,
                        "decision": d,
                    }
                    for a, i, r, d in results
                ],
                "finalized_at": now.isoformat(),
            },
        )
    )
    scope = current_scope()
    await session.execute(_BOOK_LOCK, {"key": f"decision_book:{scope.site_id if scope else ''}"})
    last = (await session.scalar(select(func.max(DecisionBookEntry.number)))) or 0
    numbers: list[int] = []
    for agenda_item, _, result, decision in results:
        if not result["accepted"]:
            continue
        last += 1
        numbers.append(last)
        session.add(
            DecisionBookEntry(
                number=last,
                meeting_id=meeting.id,
                agenda_item_id=agenda_item.id,
                decided_on=local_day(meeting.scheduled_at),
                meeting_title=meeting.title,
                item_title=agenda_item.title,
                decision=decision,
                result="accepted",
                type_name=result["type"]["name"],
            )
        )
    assembly.minutes_finalized_at = now
    assembly.minutes_finalized_by = by
    await session.flush()
    state = await load_state(session, meeting, today=today, assembly=assembly, settings=settings)
    return Finalized(state, numbers)


def summary_draft(state: State) -> dict[str, str] | None:
    """Malik ve kiracılara karar özeti duyurusu taslağı (TOP-04 adım 6) — Duyurular'dan yayın."""
    if state.minutes is None:
        return None
    lines = [
        f"{i['order']}. {i['title']}: {'KABUL' if i['result']['accepted'] else 'RET'} — "
        f"{i['decision']}"
        for i in state.minutes.content["items"]
    ]
    title = f"{state.meeting.title}: karar özeti"
    return {
        "title": title if len(title) <= rules.INVITATION_TITLE_MAX else title[:119] + "…",
        "body": f"{local_day(state.meeting.scheduled_at):%d.%m.%Y} tarihli toplantıda alınan "
        "kararlar:\n\n" + "\n".join(lines) + "\n\nTutanağın tamamı yönetimde incelenebilir.",
    }


async def attach_signed(
    session: AsyncSession, meeting: Meeting, document_id: uuid.UUID
) -> Assembly:
    assembly = await find(session, meeting.id, lock=True)
    if assembly is None or assembly.minutes_finalized_at is None:
        raise conflict("not_finalized", "Önce tutanağı kesinleştirin.")
    assembly.signed_document_id = document_id
    await session.flush()
    return assembly


async def ensure_signable(session: AsyncSession, meeting: Meeting) -> None:
    assembly = await find(session, meeting.id)
    if assembly is None or assembly.minutes_finalized_at is None:
        raise conflict("not_finalized", "Önce tutanağı kesinleştirin.")


def decision_book_query() -> Select[DecisionBookEntry]:
    return select(DecisionBookEntry).order_by(DecisionBookEntry.number.desc())


async def assembly_started(session: AsyncSession, meeting_id: uuid.UUID) -> bool:
    """Hazirunu ya da oylaması başlamış genel kurul — elle karar girişi kapalı."""
    assembly = await find(session, meeting_id)
    if assembly is None:
        return False
    attended = await session.scalar(
        select(func.count())
        .select_from(AssemblyAttendance)
        .where(AssemblyAttendance.assembly_id == assembly.id)
    )
    voted = await session.scalar(
        select(func.count())
        .select_from(AssemblyItem)
        .where(
            AssemblyItem.assembly_id == assembly.id,
            AssemblyItem.status != VoteStatus.NONE.value,
        )
    )
    return bool(attended or voted)
