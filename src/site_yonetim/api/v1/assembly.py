"""Genel kurul — servis istekleri 75–77 (Yazılım Dokümanı TOP-01, TOP-03 salonda oylama, TOP-04).

Modül `general-assembly` (kapalıysa 404). Okuma `meetings.read`; yazma `meetings.manage`.
Yönetim kurulu toplantısında 409 `not_general`. Tüm yazmalar denetim kaydında; oy olayları,
kesinleşen tutanak ve karar defteri değişmez.

- `GET  /meetings/{id}/assembly` — dosya (hazırlık, çağrı, malik listesi, hazirun, oylar, tutanak,
  ayarlar, nisap). Okuma hiçbir şey yazmaz.
- 75: `PUT …/assembly/prep`, `POST …/assembly/legal-ack`, `POST …/assembly/invitation`,
  `GET …/assembly/invitation-proof` (çağrı ispat raporu: teslim ve okuma kayıtları, sayfalı).
- 76: `PUT …/assembly/attendance`, `POST …/assembly/items/{item}/open|close`,
  `PUT …/assembly/items/{item}/ballots`.
- 77: `PUT …/assembly/minutes`, `POST …/assembly/minutes/finalize`,
  `POST …/assembly/minutes/signed` (multipart, belge arşivine), `GET /decision-book`.
- `GET|PUT /assembly-settings` — karar türleri, nisap, vekâlet sınırı ve hukuki parametreler.
"""

import datetime as dt
import uuid
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.expenses import StoreDep
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain import assembly as rules
from site_yonetim.domain.access import Permission
from site_yonetim.domain.assembly import (
    AssemblyError,
    AssemblyFormat,
    AttendanceMode,
    Ballot,
    DecisionBase,
    PersonRule,
    VoteStatus,
)
from site_yonetim.domain.documents import DocumentFieldError, DocumentKind, DocumentRuleError
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Meeting
from site_yonetim.services import assembly as svc
from site_yonetim.services import documents as documents_svc
from site_yonetim.services import subject_documents

router = APIRouter(prefix="/sites/{slug}", tags=["genel kurul"])
Paging = Annotated[PageParams, Depends()]
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.GENERAL_ASSEMBLY))]


async def _reader(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.MEETINGS_READ):
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.MEETINGS_MANAGE):
        raise ForbiddenError
    return ctx


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]


def _error(exc: AssemblyError) -> ApiError:
    if exc.code == "validation_error":
        return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, FORM_ERROR, exc.fields)
    return ApiError(HTTPStatus(exc.status), exc.code, exc.message, exc.fields)


async def _meeting(ctx: SiteContext, meeting_id: uuid.UUID, *, lock: bool = False) -> Meeting:
    try:
        meeting = await svc.general_meeting(ctx.session, meeting_id, lock=lock)
    except AssemblyError as exc:
        raise _error(exc) from exc
    if meeting is None:
        raise NotFoundError("Toplantı bulunamadı.")
    return meeting


# --- Şemalar ----------------------------------------------------------------------------------


class AssemblyThreshold(BaseModel):
    num: int = Field(ge=1, le=100)
    den: int = Field(ge=1, le=100)
    strict: bool = Field(description="true: oranın fazlası (>), false: en az (≥)")
    person: bool = Field(description="kişi sayısı aranır")
    share: bool = Field(description="arsa payı aranır")


class AssemblyDecisionType(BaseModel):
    code: str
    name: str
    base: DecisionBase = Field(description="present: katılanlar · all: tüm malikler")
    num: int
    den: int
    strict: bool
    person: bool
    share: bool
    abstain_in_base: bool = Field(
        default=True, description="çekimser oy tabanda kalır (kabul sayılmaz — fiilen ret etkisi)"
    )


class AssemblySettingsOut(BaseModel):
    decision_types: list[AssemblyDecisionType]
    quorum: AssemblyThreshold
    second_call_quorum: AssemblyThreshold | None = Field(
        description="ikinci toplantıda aranan nisap; null: aranmaz"
    )
    proxy_limit: int = Field(description="kişi başı azami vekâlet (malik)")
    person_rule: PersonRule
    notice_days: int = Field(description="çağrı süresi (gün) — uyarı; sunucu engellemez")
    second_call_days: int
    read_ack_proof: bool = Field(description="okuma onayı çağrı ispatı sayılır")
    remote_voting: bool = Field(description="uzaktan oy (B18) — varsayılan kapalı")
    digital_proxy: bool = Field(description="dijital vekâlet (B18) — varsayılan kapalı")
    legal_basis_document_id: uuid.UUID | None
    sms_verification_available: bool
    updated_at: dt.datetime | None
    updated_by: str | None


class AssemblySettingsIn(BaseModel):
    """Verilmeyen alan değişmez."""

    decision_types: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    quorum: dict[str, Any] | None = None
    second_call_quorum: dict[str, Any] | None = None
    proxy_limit: int | None = None
    person_rule: str | None = Field(default=None, max_length=20)
    notice_days: int | None = None
    second_call_days: int | None = None
    read_ack_proof: bool | None = None
    remote_voting: bool | None = None
    digital_proxy: bool | None = None
    legal_basis_document_id: uuid.UUID | None = None


def _settings_out(s: svc.Settings) -> AssemblySettingsOut:
    return AssemblySettingsOut(
        decision_types=[AssemblyDecisionType(**t.as_dict()) for t in s.decision_types],
        quorum=AssemblyThreshold(**s.quorum.as_dict()),
        second_call_quorum=(
            AssemblyThreshold(**s.second_call_quorum.as_dict()) if s.second_call_quorum else None
        ),
        proxy_limit=s.proxy_limit,
        person_rule=s.person_rule,
        notice_days=s.notice_days,
        second_call_days=s.second_call_days,
        read_ack_proof=s.read_ack_proof,
        remote_voting=s.remote_voting,
        digital_proxy=s.digital_proxy,
        legal_basis_document_id=s.legal_basis_document_id,
        sms_verification_available=rules.SMS_VERIFICATION_AVAILABLE,
        updated_at=s.updated_at,
        updated_by=s.updated_by,
    )


class AssemblyLegalAck(BaseModel):
    by: str
    at: dt.datetime


class AssemblyInvitationOut(BaseModel):
    announcement_id: uuid.UUID
    sent_at: dt.datetime
    by: str | None
    ack_hours: int
    ack_deadline: dt.datetime
    recipients: int = Field(description="çağrının gittiği malik (kişi)")
    read: int
    read_in_time: int = Field(description="okuma onayı süresi içinde okuyan")
    counts_as_proof: bool = Field(description="site ayarı: okuma onayı çağrı ispatı sayılır")


class AssemblyRosterUnitOut(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    block_name: str
    owners: list[str]
    key: str = Field(description="kişi anahtarı — aynı anahtar aynı malik (tek oy)")
    share: int
    share_den: int


class AssemblyAttendanceOut(BaseModel):
    unit_id: uuid.UUID
    mode: AttendanceMode
    proxy_name: str | None
    at: dt.datetime
    recorded_by: str | None


class AssemblyPersonCounts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    for_: int = Field(alias="for")
    against: int
    abstain: int
    base: int


class AssemblyShareCounts(BaseModel):
    """Malik listesinin toplam arsa payına oran — metin, 0–1, 6 ondalık (`"0.385200"`)."""

    model_config = ConfigDict(populate_by_name=True)
    for_: str = Field(alias="for")
    against: str
    abstain: str
    base: str


class AssemblyVoteResult(BaseModel):
    type_code: str
    type_name: str
    persons: AssemblyPersonCounts
    shares: AssemblyShareCounts
    person_ok: bool | None = Field(description="null: ölçüt aranmıyor")
    share_ok: bool | None
    accepted: bool


class AssemblyItemVote(BaseModel):
    decision_type: str
    status: VoteStatus
    ballots: dict[str, Ballot] = Field(description="bölüm kimliği → geçerli oy")
    result: AssemblyVoteResult | None = Field(description="kapanış sonucu (değişmez)")
    live: AssemblyVoteResult | None = Field(description="açık oylamada anlık sayım")
    opened_at: dt.datetime | None
    closed_at: dt.datetime | None


class AssemblyQuorum(BaseModel):
    persons_present: int
    persons_total: int
    share_present: str = Field(description="toplam paya oran — metin, 0–1, 6 ondalık")
    share_total: str = Field(description='"1.000000" (malik listesinde arsa payı yoksa "0.000000")')
    person_ok: bool
    share_ok: bool
    required: bool = Field(description="false: ikinci toplantıda nisap aranmıyor")
    ok: bool
    second_call: bool


class AssemblyNotice(BaseModel):
    days: int
    last_day: dt.date = Field(description="çağrının en geç gönderileceği gün")
    ok: bool | None = Field(description="gönderildiyse süreye uyuldu mu; gönderilmediyse null")


class AssemblySignedDocument(BaseModel):
    id: uuid.UUID
    title: str
    version: int


class AssemblySummaryDraft(BaseModel):
    title: str
    body: str


class AssemblyMinutesOut(BaseModel):
    notes: dict[str, str]
    decisions: dict[str, str]
    opening: str
    attendance_sentence: str
    finalized_at: dt.datetime | None
    finalized_by: str | None
    signed_document: AssemblySignedDocument | None
    summary_draft: AssemblySummaryDraft | None = Field(
        description="kesinleşince: malik ve kiracılara karar özeti duyurusu taslağı"
    )


class AssemblyOut(BaseModel):
    meeting_id: uuid.UUID
    format: AssemblyFormat
    format_label: str
    legal_ack: AssemblyLegalAck | None
    second_call: bool
    item_types: dict[str, str]
    invitation: AssemblyInvitationOut | None
    notice: AssemblyNotice
    roster: list[AssemblyRosterUnitOut]
    roster_frozen: bool
    attendance: list[AssemblyAttendanceOut]
    votes: dict[str, AssemblyItemVote]
    minutes: AssemblyMinutesOut
    settings: AssemblySettingsOut
    quorum: AssemblyQuorum


def _ratio(value: Fraction, total: Fraction = Fraction(1)) -> str:
    """Oran metni (Decimal, 6 ondalık, ROUND_HALF_UP) — kayan nokta yok."""
    if total <= 0:
        return "0.000000"
    ratio = value / total
    exact = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    return str(exact.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _result(raw: dict[str, Any]) -> AssemblyVoteResult:
    persons, shares = raw["persons"], raw["shares"]
    return AssemblyVoteResult(
        type_code=raw["type"]["code"],
        type_name=raw["type"]["name"],
        persons=AssemblyPersonCounts.model_validate(persons),
        shares=AssemblyShareCounts.model_validate(
            {k: _ratio(Fraction(v)) for k, v in shares.items()}
        ),
        person_ok=raw["person_ok"],
        share_ok=raw["share_ok"],
        accepted=raw["accepted"],
    )


def assembly_out(state: svc.State) -> AssemblyOut:
    a, s = state.assembly, state.settings
    q = state.quorum()
    total = q.share_total
    votes: dict[str, AssemblyItemVote] = {}
    for agenda_item in state.agenda:
        item = state.items.get(agenda_item.id)
        status = VoteStatus(item.status) if item else VoteStatus.NONE
        code = state.item_type(agenda_item.id)
        live = None
        if item is not None and status is VoteStatus.OPEN:
            dtype = s.decision_type(code)
            live = _result(svc.result_snapshot(state, agenda_item.id, dtype)) if dtype else None
        votes[str(agenda_item.id)] = AssemblyItemVote(
            decision_type=code,
            status=status,
            ballots={str(k): v for k, v in state.ballots.get(agenda_item.id, {}).items()},
            result=_result(item.result) if item is not None and item.result else None,
            live=live,
            opened_at=item.opened_at if item else None,
            closed_at=item.closed_at if item else None,
        )
    meeting_on = svc.local_day(state.meeting.scheduled_at)
    last_day = rules.notice_last_day(meeting_on, s.notice_days)
    invitation = None
    if a is not None and a.invitation_sent_at is not None and a.invitation_announcement_id:
        invitation = AssemblyInvitationOut(
            announcement_id=a.invitation_announcement_id,
            sent_at=a.invitation_sent_at,
            by=a.invitation_by_name,
            ack_hours=a.invitation_ack_hours or 0,
            ack_deadline=svc.ack_deadline(a) or a.invitation_sent_at,
            recipients=state.invitation_total,
            read=state.invitation_read,
            read_in_time=state.invitation_read_in_time,
            counts_as_proof=s.read_ack_proof,
        )
    fmt = AssemblyFormat(a.format) if a else s.default_format
    finalized = a is not None and a.minutes_finalized_at is not None
    doc = state.signed_document
    draft = svc.summary_draft(state) if finalized else None
    return AssemblyOut(
        meeting_id=state.meeting.id,
        format=fmt,
        format_label=rules.FORMAT_LABELS[fmt],
        legal_ack=(
            AssemblyLegalAck(by=a.legal_ack_by_name or "", at=a.legal_ack_at)
            if a is not None and a.legal_ack_at is not None
            else None
        ),
        second_call=bool(a and a.second_call),
        item_types={str(i.id): state.item_type(i.id) for i in state.agenda},
        invitation=invitation,
        notice=AssemblyNotice(
            days=s.notice_days,
            last_day=last_day,
            ok=(svc.local_day(invitation.sent_at) <= last_day) if invitation else None,
        ),
        roster=[
            AssemblyRosterUnitOut(
                unit_id=r.unit_id,
                unit_name=r.unit_name,
                block_name=r.block_name,
                owners=list(r.owners),
                key=r.key,
                share=r.share_num,
                share_den=r.share_den,
            )
            for r in state.roster
        ],
        roster_frozen=bool(a and a.roster_frozen_at),
        attendance=[
            AssemblyAttendanceOut(
                unit_id=x.unit_id,
                mode=AttendanceMode(x.mode),
                proxy_name=x.proxy_name,
                at=x.updated_at or x.created_at,
                recorded_by=x.recorded_by_name,
            )
            for x in sorted(state.attendance, key=lambda x: x.created_at)
        ],
        votes=votes,
        minutes=AssemblyMinutesOut(
            notes={str(k): i.minutes_note for k, i in state.items.items() if i.minutes_note},
            decisions={str(k): i.decision_text for k, i in state.items.items() if i.decision_text},
            opening=(a.minutes_opening or "") if a else "",
            attendance_sentence=svc.attendance_sentence(state),
            finalized_at=a.minutes_finalized_at if a else None,
            finalized_by=a.minutes_finalized_by if a else None,
            signed_document=(
                AssemblySignedDocument(id=doc.id, title=doc.title, version=doc.current_version)
                if doc is not None
                else None
            ),
            summary_draft=AssemblySummaryDraft(**draft) if draft else None,
        ),
        settings=_settings_out(s),
        quorum=AssemblyQuorum(
            persons_present=q.persons_present,
            persons_total=q.persons_total,
            share_present=_ratio(q.share_present, total),
            share_total=_ratio(total, total),
            person_ok=q.person_ok,
            share_ok=q.share_ok,
            required=q.required,
            ok=q.ok,
            second_call=bool(a and a.second_call),
        ),
    )


async def _written(ctx: SiteContext, state: svc.State, message: str) -> Written[AssemblyOut]:
    out = assembly_out(state)
    await ctx.session.commit()
    return Written(data=out, message=message)


# --- Ayarlar ----------------------------------------------------------------------------------


@router.get("/assembly-settings", summary="Genel kurul ayarları (karar türleri, nisap, vekâlet)")
async def get_assembly_settings(ctx: Reader) -> AssemblySettingsOut:
    return _settings_out(await svc.load_settings(ctx.session))


@router.put("/assembly-settings", summary="Genel kurul ayarlarını kaydet (verilen alanlar)")
async def put_assembly_settings(
    body: AssemblySettingsIn, ctx: Manager, current: CurrentUserDep
) -> Written[AssemblySettingsOut]:
    given = body.model_dump(exclude_unset=True)
    try:
        saved = await svc.save_settings(
            ctx.session, svc.SettingsInput(**given), by=current.user.full_name
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    out = _settings_out(saved)
    await ctx.session.commit()
    return Written(data=out, message="Genel kurul ayarları kaydedildi.")


# --- Dosya ------------------------------------------------------------------------------------


@router.get("/meetings/{meeting_id}/assembly", summary="Genel kurul dosyası")
async def get_assembly(meeting_id: uuid.UUID, ctx: Reader, today: TodayDep) -> AssemblyOut:
    meeting = await _meeting(ctx, meeting_id)
    return assembly_out(await svc.load_state(ctx.session, meeting, today=today))


class AssemblyPrepIn(BaseModel):
    format: str | None = Field(default=None, max_length=20)
    item_types: dict[str, str] | None = Field(default=None, max_length=60)
    second_call: bool | None = None


@router.put(
    "/meetings/{meeting_id}/assembly/prep", summary="Format, karar türleri, ikinci toplantı"
)
async def put_assembly_prep(
    meeting_id: uuid.UUID, body: AssemblyPrepIn, ctx: Manager, today: TodayDep
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state = await svc.prepare(
            ctx.session,
            meeting,
            today=today,
            fmt=body.format,
            item_types=body.item_types,
            second_call=body.second_call,
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, "Toplantı hazırlığı kaydedildi.")


@router.post(
    "/meetings/{meeting_id}/assembly/legal-ack", summary="Hukuki geçerlilik uyarısını onayla"
)
async def post_assembly_legal_ack(
    meeting_id: uuid.UUID, ctx: Manager, current: CurrentUserDep, now: NowDep, today: TodayDep
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state = await svc.legal_ack(
            ctx.session, meeting, today=today, by=current.user.full_name, now=now
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, "Hukuki geçerlilik uyarısı onaylandı.")


class AssemblyInvitationIn(BaseModel):
    title: str | None = Field(default=None, max_length=400)
    body: str | None = Field(default=None, max_length=20_000)
    ack_hours: int | None = None


@router.post(
    "/meetings/{meeting_id}/assembly/invitation",
    summary="Çağrıyı maliklere gönder (uygulama + e-posta + SMS; okuma onaylı)",
)
async def post_assembly_invitation(
    meeting_id: uuid.UUID,
    body: AssemblyInvitationIn,
    ctx: Manager,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        sent = await svc.send_invitation(
            ctx.session,
            meeting,
            title=body.title,
            body=body.body,
            ack_hours=body.ack_hours,
            by=current.user.full_name,
            now=now,
            today=today,
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    message = (
        f"Çağrı {sent.recipients} maliğe okuma onaylı duyuru olarak gönderildi; ispat raporu "
        "duyurunun teslim ve okuma kayıtlarıdır. E-posta/SMS gönderimi sağlayıcı seçilince "
        "yapılacak; teslim kayıtları bekliyor."
    )
    if sent.notice_short:
        message += (
            f" Uyarı: çağrı süresi ({sent.state.settings.notice_days} gün) için son gün "
            f"{sent.last_day:%d.%m.%Y} idi."
        )
    return await _written(ctx, sent.state, message)


class AssemblyProofRow(BaseModel):
    person_id: uuid.UUID
    name: str
    units: list[str]
    channels: list[str] = Field(description="teslim kaydı olan kanallar")
    sent_at: dt.datetime | None = Field(description="uygulama içi teslim")
    read_at: dt.datetime | None
    read_in_time: bool


@router.get(
    "/meetings/{meeting_id}/assembly/invitation-proof",
    summary="Çağrı ispat raporu (teslim ve okuma kayıtları)",
)
async def get_assembly_invitation_proof(
    meeting_id: uuid.UUID, ctx: Reader, paging: Paging, today: TodayDep
) -> Page[AssemblyProofRow]:
    meeting = await _meeting(ctx, meeting_id)
    assembly = await svc.find(ctx.session, meeting.id)
    if assembly is None or assembly.invitation_announcement_id is None:
        raise NotFoundError("Bu toplantının çağrısı henüz gönderilmedi.")
    query = svc.proof_query(assembly.invitation_announcement_id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    people = list(await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size)))
    rows = await svc.proof_rows(ctx.session, assembly, people, today=today)
    return Page(
        items=[_proof(r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


def _proof(r: svc.ProofRow) -> AssemblyProofRow:
    return AssemblyProofRow(
        person_id=r.person_id,
        name=r.name,
        units=r.units,
        channels=r.channels,
        sent_at=r.sent_at,
        read_at=r.read_at,
        read_in_time=r.read_in_time,
    )


# --- 76 Hazirun ve oylama -----------------------------------------------------------------------


class AssemblyAttendanceIn(BaseModel):
    unit_id: uuid.UUID
    mode: str | None = Field(default=None, max_length=10, description="self · proxy · null (çıkar)")
    proxy_name: str | None = Field(default=None, max_length=300)


@router.put(
    "/meetings/{meeting_id}/assembly/attendance", summary="Hazirun: kendisi / vekil / çıkar"
)
async def put_assembly_attendance(
    meeting_id: uuid.UUID,
    body: AssemblyAttendanceIn,
    ctx: Manager,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state, message = await svc.set_attendance(
            ctx.session,
            meeting,
            unit_id=body.unit_id,
            mode=body.mode,
            proxy_name=body.proxy_name,
            by=current.user.full_name,
            now=now,
            today=today,
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, message)


@router.post("/meetings/{meeting_id}/assembly/items/{item_id}/open", summary="Oylamayı aç")
async def post_assembly_item_open(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: Manager,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state = await svc.open_vote(
            ctx.session, meeting, item_id, by=current.user.full_name, now=now, today=today
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, "Oylama açıldı.")


@router.post("/meetings/{meeting_id}/assembly/items/{item_id}/close", summary="Oylamayı kapat")
async def post_assembly_item_close(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: Manager,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state, accepted = await svc.close_vote(
            ctx.session, meeting, item_id, by=current.user.full_name, now=now, today=today
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, f"Oylama kapandı: {'KABUL' if accepted else 'RET'}.")


class AssemblyBallotsIn(BaseModel):
    ballots: dict[str, str | None] = Field(
        max_length=20_000, description="bölüm kimliği → for · against · abstain · null (sil)"
    )


@router.put("/meetings/{meeting_id}/assembly/items/{item_id}/ballots", summary="Salonda oyları gir")
async def put_assembly_ballots(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    body: AssemblyBallotsIn,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state = await svc.record_ballots(
            ctx.session,
            meeting,
            item_id,
            ballots=body.ballots,
            user_id=current.user.id,
            by=current.user.full_name,
            today=today,
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, "Oylar kaydedildi.")


# --- 77 Tutanak ve karar defteri ---------------------------------------------------------------


class AssemblyMinutesIn(BaseModel):
    notes: dict[str, str] | None = Field(default=None, max_length=60)
    decisions: dict[str, str] | None = Field(default=None, max_length=60)
    opening: str | None = Field(default=None, max_length=10_000)


@router.put("/meetings/{meeting_id}/assembly/minutes", summary="Tutanak taslağını kaydet")
async def put_assembly_minutes(
    meeting_id: uuid.UUID, body: AssemblyMinutesIn, ctx: Manager, today: TodayDep
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        state = await svc.save_minutes(
            ctx.session,
            meeting,
            notes=body.notes,
            decisions=body.decisions,
            opening=body.opening,
            today=today,
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    return await _written(ctx, state, "Tutanak taslağı kaydedildi.")


@router.post(
    "/meetings/{meeting_id}/assembly/minutes/finalize",
    summary="Tutanağı kesinleştir (kararlar toplantıya ve karar defterine; değişmez)",
)
async def post_assembly_finalize(
    meeting_id: uuid.UUID, ctx: Manager, current: CurrentUserDep, now: NowDep, today: TodayDep
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        done = await svc.finalize(
            ctx.session, meeting, by=current.user.full_name, now=now, today=today
        )
    except AssemblyError as exc:
        raise _error(exc) from exc
    count = len(done.book_numbers)
    message = (
        f"Tutanak kesinleşti; {count} karar karar defterine işlendi"
        + (f" (no {done.book_numbers[0]}–{done.book_numbers[-1]})." if count > 1 else "")
        + (f" (no {done.book_numbers[0]})." if count == 1 else "")
        + ("." if count == 0 else "")
    )
    return await _written(ctx, done.state, message)


@router.post(
    "/meetings/{meeting_id}/assembly/minutes/signed",
    summary="İmzalı tutanağı yükle (multipart) — belge arşivine",
)
async def post_assembly_signed(
    meeting_id: uuid.UUID,
    ctx: Manager,
    current: CurrentUserDep,
    store: StoreDep,
    today: TodayDep,
    file: Annotated[UploadFile | None, File(description="PDF, PNG, JPEG, WebP · ≤ 10 MB")] = None,
    title: Annotated[str | None, Form(max_length=200)] = None,
) -> Written[AssemblyOut]:
    meeting = await _meeting(ctx, meeting_id, lock=True)
    try:
        await svc.ensure_signable(ctx.session, meeting)
    except AssemblyError as exc:
        raise _error(exc) from exc
    data = await file.read(MAX_FILE_BYTES + 1) if file is not None else b""
    label = " ".join((title or "İmzalı tutanak").split())
    written: str | None = None
    try:
        uploaded = await subject_documents.attach(
            ctx.session,
            store,
            subject_type="meetings",
            subject_id=meeting.id,
            title=f"Toplantı {meeting.number} — {meeting.title}: {label}",
            file_name=file.filename if file is not None else None,
            data=data,
            uploader=documents_svc.Uploader(current.user.id, current.user.full_name, False),
            kind=DocumentKind.MINUTES,
        )
        written = uploaded.storage_path
        await svc.attach_signed(ctx.session, meeting, uploaded.document.id)
        state = await svc.load_state(ctx.session, meeting, today=today)
        out = assembly_out(state)
        await ctx.session.commit()
    except DocumentFieldError as exc:
        if written:
            store.delete(written)
        fields = {("file" if k in {"file", "title"} else k): v for k, v in exc.fields.items()}
        raise ApiError(422, "validation_error", FORM_ERROR, fields) from exc
    except DocumentRuleError as exc:
        if written:
            store.delete(written)
        raise ApiError(409, exc.code, exc.message) from exc
    except BaseException:
        if written:
            store.delete(written)
        raise
    return Written(
        data=out,
        message="İmzalı tutanak belge arşivine kaydedildi."
        if uploaded.is_new
        else f"İmzalı tutanağın {uploaded.version}. sürümü kaydedildi.",
    )


class DecisionBookEntryOut(BaseModel):
    no: int
    meeting_id: uuid.UUID
    meeting_title: str
    date: dt.date
    item_title: str
    decision: str
    result: str
    type_name: str


@router.get("/decision-book", summary="Karar defteri (sıra numarası büyükten küçüğe)")
async def list_decision_book(ctx: Reader, paging: Paging) -> Page[DecisionBookEntryOut]:
    query = svc.decision_book_query()
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size))
    return Page(
        items=[
            DecisionBookEntryOut(
                no=r.number,
                meeting_id=r.meeting_id,
                meeting_title=r.meeting_title,
                date=r.decided_on,
                item_title=r.item_title,
                decision=r.decision,
                result=r.result,
                type_name=r.type_name,
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )
