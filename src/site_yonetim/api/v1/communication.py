"""İletişim ve acil durum uçları — servis istekleri 72–74 (Yazılım Dokümanı ILT-02, ILT-04,
ILT-06). Modül `announcements` (kapalıysa 404).

- Şablonlar: `announcements.publish`.
- Acil durum başlatma/bitirme: `announcements.publish` ya da `security.incidents`; pano ve
  yoklama ayrıca `security.visitors`. Başlatma onay gerektirmez, denetim kaydına yazılır.
- Acil bilgi kartı: yazma `announcements.publish`; okuma personel ve sakin. Okuma uçları
  `ETag` döner, `If-None-Match` ile 304 (uygulama/PWA önbelleği kartı ucuza tazeler).
- Saklama ayarı (K8): okuma acil durum yetkilisi, yazma `announcements.publish`.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.requests import rule_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.communication import (
    EMERGENCY_LABELS,
    SYSTEM_BY_CODE,
    EmergencyKind,
    EmergencyScope,
    TemplateChannel,
    UnitSafety,
    Variant,
    sms_count,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Emergency
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import communication as comm_svc
from site_yonetim.services import emergencies as em_svc

router = APIRouter(prefix="/sites/{slug}", tags=["iletişim"])
resident_router = APIRouter(prefix="/sites/{slug}/resident", tags=["sakin"])
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.ANNOUNCEMENTS))]


def _staff(ctx: SiteContext) -> None:
    if ctx.access.person_id is not None and not ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH):
        raise ForbiddenError  # sakin kendi ucunu kullanır


async def _publisher(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH):
        raise ForbiddenError
    return ctx


async def _starter(ctx: Module) -> SiteContext:
    if not (
        ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH)
        or ctx.access.can(Permission.SECURITY_INCIDENTS)
    ):
        raise ForbiddenError
    return ctx


async def _board(ctx: Module) -> SiteContext:
    if not (
        ctx.access.can(Permission.ANNOUNCEMENTS_PUBLISH)
        or ctx.access.can(Permission.SECURITY_INCIDENTS)
        or ctx.access.can(Permission.SECURITY_VISITORS)
    ):
        raise ForbiddenError
    return ctx


async def _reader(ctx: Module) -> SiteContext:
    _staff(ctx)
    return ctx


async def _resident(ctx: Module) -> SiteContext:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx


Publisher = Annotated[SiteContext, Depends(_publisher)]
Starter = Annotated[SiteContext, Depends(_starter)]
Board = Annotated[SiteContext, Depends(_board)]
Reader = Annotated[SiteContext, Depends(_reader)]
Resident = Annotated[SiteContext, Depends(_resident)]


# --- 72 Şablonlar -------------------------------------------------------------------------


class TemplateVariantIO(BaseModel):
    lang: str = Field(max_length=5, description="tr · en")
    subject: str | None = Field(
        default=None, max_length=200, description="duyuru şablonunda başlık"
    )
    body: str = Field(max_length=4000)


class TemplateHistoryOut(BaseModel):
    version: int
    at: dt.datetime | None = Field(description="sistem sürümünde null")
    by: str


class SmsCountOut(BaseModel):
    lang: str
    encoding: str = Field(description="GSM-7 · Unicode")
    chars: int
    segments: int


class NotificationTemplateOut(BaseModel):
    code: str
    name: str
    channel: TemplateChannel
    variables: list[str]
    required: list[str]
    variants: list[TemplateVariantIO]
    version: int
    history: list[TemplateHistoryOut]
    customized: bool
    sms: list[SmsCountOut] | None = Field(description="yalnız SMS şablonunda")


def _template_out(t: comm_svc.TemplateState) -> NotificationTemplateOut:
    variants = [TemplateVariantIO(lang=v.lang, subject=v.subject, body=v.body) for v in t.variants]
    sms = None
    if t.defn.channel is TemplateChannel.SMS:
        sms = []
        for v in t.variants:
            c = sms_count(v.body)
            sms.append(
                SmsCountOut(lang=v.lang, encoding=c.encoding, chars=c.chars, segments=c.segments)
            )
    return NotificationTemplateOut(
        code=t.defn.code,
        name=t.defn.name,
        channel=t.defn.channel,
        variables=list(t.defn.variables),
        required=list(t.defn.required),
        variants=variants,
        version=t.version,
        history=[TemplateHistoryOut(version=v, at=at, by=by) for v, at, by in t.history],
        customized=t.customized,
        sms=sms,
    )


async def _template(ctx: SiteContext, code: str) -> NotificationTemplateOut:
    for t in await comm_svc.templates(ctx.session):
        if t.defn.code == code:
            return _template_out(t)
    raise NotFoundError("Şablon bulunamadı.")  # pragma: no cover — kod önceden doğrulandı


@router.get("/notification-templates", summary="Bildirim ve duyuru şablonları")
async def list_templates(ctx: Publisher) -> list[NotificationTemplateOut]:
    return [_template_out(t) for t in await comm_svc.templates(ctx.session)]


class TemplateIn(BaseModel):
    variants: list[TemplateVariantIO] = Field(max_length=7)


@router.put("/notification-templates/{code}", summary="Şablonu kaydet (yeni sürüm)")
async def save_template(
    code: str, body: TemplateIn, ctx: Publisher, current: CurrentUserDep
) -> Written[NotificationTemplateOut]:
    try:
        version = await comm_svc.save_template(
            ctx.session,
            code,
            [Variant(v.lang, v.subject, v.body) for v in body.variants],
            by=current.user.full_name,
        )
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    name = SYSTEM_BY_CODE[code].name
    return Written(
        data=await _template(ctx, code),
        message=f"{name} şablonu kaydedildi (sürüm {version}); yeni gönderimler bu sürümü "
        "kullanır.",
    )


@router.post("/notification-templates/{code}/reset", summary="Sistem metnine dön (yeni sürüm)")
async def reset_template(
    code: str, ctx: Publisher, current: CurrentUserDep
) -> Written[NotificationTemplateOut]:
    try:
        await comm_svc.reset_template(ctx.session, code, by=current.user.full_name)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    await ctx.session.commit()
    return Written(data=await _template(ctx, code), message="Sistem şablonuna dönüldü.")


# --- 74 Acil bilgi kartı ------------------------------------------------------------------


class MusterPointIO(BaseModel):
    id: str | None = Field(default=None, max_length=40)
    name: str = Field(default="", max_length=120)
    directions: str = Field(default="", max_length=400)
    map: str | None = Field(default=None, description="küçültülmüş görsel (data URL)")


class EmergencyPhoneIO(BaseModel):
    label: str = Field(default="", max_length=80)
    number: str = Field(default="", max_length=30)


class BlockNoteIO(BaseModel):
    block_id: str = Field(max_length=40)
    block_name: str | None = Field(default=None, max_length=120)
    note: str = Field(default="", max_length=400)


class EmergencyInfoCardOut(BaseModel):
    muster_points: list[MusterPointIO]
    phones: list[EmergencyPhoneIO]
    instructions: dict[str, str] = Field(description="earthquake · fire · flood · gas")
    block_notes: list[BlockNoteIO]
    version: int = Field(description="0 = henüz yayınlanmadı (varsayılanlar)")
    updated_at: dt.datetime | None
    updated_by: str | None


class EmergencyInfoCardIn(BaseModel):
    muster_points: list[MusterPointIO] = Field(default_factory=list, max_length=50)
    phones: list[EmergencyPhoneIO] = Field(default_factory=list, max_length=50)
    instructions: dict[str, str] = Field(default_factory=dict)
    block_notes: list[BlockNoteIO] = Field(default_factory=list, max_length=500)


def _card_out(c: comm_svc.Card) -> EmergencyInfoCardOut:
    return EmergencyInfoCardOut(
        muster_points=[MusterPointIO(**m) for m in c.muster_points],
        phones=[EmergencyPhoneIO(**p) for p in c.phones],
        instructions=c.instructions,
        block_notes=[BlockNoteIO(**n) for n in c.block_notes],
        version=c.version,
        updated_at=c.updated_at,
        updated_by=c.updated_by,
    )


async def _card_response(ctx: SiteContext, if_none_match: str | None) -> Response:
    c = await comm_svc.card(ctx.session)
    etag = f'"acil-kart-{ctx.site.id}-{c.version}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match and etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
    return JSONResponse(_card_out(c).model_dump(mode="json"), headers=headers)


IfNoneMatch = Annotated[str | None, Header(max_length=400)]
_NOT_MODIFIED: dict[int | str, dict[str, object]] = {
    304: {"description": "Kart değişmedi (If-None-Match)"}
}


@router.get(
    "/emergency-info-card",
    summary="Acil bilgi kartı (personel)",
    response_model=EmergencyInfoCardOut,
    responses=_NOT_MODIFIED,
)
async def info_card(ctx: Reader, if_none_match: IfNoneMatch = None) -> Response:
    return await _card_response(ctx, if_none_match)


@resident_router.get(
    "/emergency-info-card",
    summary="Acil bilgi kartı (sakin, çevrimdışı)",
    response_model=EmergencyInfoCardOut,
    responses=_NOT_MODIFIED,
)
async def my_info_card(ctx: Resident, if_none_match: IfNoneMatch = None) -> Response:
    return await _card_response(ctx, if_none_match)


@router.put("/emergency-info-card", summary="Acil bilgi kartını yayınla (yeni sürüm)")
async def publish_card(
    body: EmergencyInfoCardIn, ctx: Publisher, current: CurrentUserDep
) -> Written[EmergencyInfoCardOut]:
    try:
        version = await comm_svc.publish_card(
            ctx.session,
            comm_svc.CardInput(
                muster_points=[m.model_dump() for m in body.muster_points],
                phones=[p.model_dump() for p in body.phones],
                instructions=body.instructions,
                block_notes=[n.model_dump() for n in body.block_notes],
            ),
            by=current.user.full_name,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=_card_out(await comm_svc.card(ctx.session)),
        message=f"Acil bilgi kartı yayınlandı (sürüm {version}); uygulamalar arka planda "
        "güncellenir.",
    )


# --- Saklama ayarı (K8) -------------------------------------------------------------------


class EmergencySettingsOut(BaseModel):
    retention_days: int = Field(description="bitişten sonra yoklama ayrıntısının saklanacağı gün")
    auto_purge: bool = Field(description="gece işi siler mi (hukuk teyidine kadar kapalı)")
    updated_by: str | None
    updated_at: dt.datetime | None


class EmergencySettingsIn(BaseModel):
    retention_days: int = Field(ge=1, le=365)
    auto_purge: bool


def _settings_out(s: comm_svc.RetentionSettings) -> EmergencySettingsOut:
    return EmergencySettingsOut(
        retention_days=s.retention_days,
        auto_purge=s.auto_purge,
        updated_by=s.updated_by,
        updated_at=s.updated_at,
    )


@router.get("/emergency-settings", summary="Acil durum verisi saklama ayarı")
async def emergency_settings(ctx: Starter) -> EmergencySettingsOut:
    return _settings_out(await comm_svc.retention(ctx.session))


@router.put("/emergency-settings", summary="Saklama ayarını kaydet")
async def save_emergency_settings(
    body: EmergencySettingsIn, ctx: Publisher, current: CurrentUserDep
) -> Written[EmergencySettingsOut]:
    try:
        await comm_svc.save_retention(
            ctx.session,
            retention_days=body.retention_days,
            auto_purge=body.auto_purge,
            by=current.user.full_name,
        )
    except FormFieldError as exc:  # pragma: no cover — şema zaten sınırlar
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=_settings_out(await comm_svc.retention(ctx.session)),
        message="Saklama ayarı kaydedildi.",
    )


# --- 73 Acil durum ------------------------------------------------------------------------


class EmergencyTotals(BaseModel):
    total: int
    safe: int
    muster: int
    help: int
    none: int


class EmergencyAuditEntry(BaseModel):
    at: dt.datetime
    by: str
    action: str


class EmergencyOut(BaseModel):
    id: uuid.UUID
    kind: EmergencyKind
    scope: EmergencyScope
    block_ids: list[uuid.UUID]
    message: str
    started_by: str
    started_at: dt.datetime
    ended_at: dt.datetime | None
    ended_by: str | None
    summary: EmergencyTotals | None = Field(description="bitişte üretilen özet")
    purge_on: dt.date | None = Field(description="yoklama ayrıntısının silineceği gün")
    purged: bool
    audit: list[EmergencyAuditEntry]


class EmergencyBoardRow(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    block_id: uuid.UUID | None
    block_name: str
    status: UnitSafety
    note: str | None
    muster_point: str | None
    by: str | None
    method: str | None = Field(description="app (sakin) · list (yoklama)")
    at: dt.datetime | None


class EmergencyBoardOut(EmergencyOut):
    rows: list[EmergencyBoardRow] = Field(description="yardım isteyenler en üstte")
    totals: EmergencyTotals


def _totals(raw: dict[str, int]) -> EmergencyTotals:
    return EmergencyTotals(**{k: raw.get(k, 0) for k in EmergencyTotals.model_fields})


def _emergency_out(e: Emergency) -> EmergencyOut:
    audit = [EmergencyAuditEntry(at=e.started_at, by=e.started_by, action="Acil durum başlatıldı")]
    if e.ended_at and e.ended_by:
        audit.append(
            EmergencyAuditEntry(at=e.ended_at, by=e.ended_by, action="Acil durum bitirildi")
        )
    return EmergencyOut(
        id=e.id,
        kind=EmergencyKind(e.kind),
        scope=EmergencyScope(e.scope),
        block_ids=list(e.block_ids or []),
        message=e.message,
        started_by=e.started_by,
        started_at=e.started_at,
        ended_at=e.ended_at,
        ended_by=e.ended_by,
        summary=_totals(e.summary) if e.summary else None,
        purge_on=e.purge_on,
        purged=e.purged_at is not None,
        audit=audit,
    )


_ORDER = {UnitSafety.HELP.value: 0, UnitSafety.NONE.value: 1}


async def _board_out(ctx: SiteContext, e: Emergency) -> EmergencyBoardOut:
    units = await em_svc.rows(ctx.session, e.id)
    rows = [
        EmergencyBoardRow(
            unit_id=u.unit_id,
            unit_name=u.unit_name,
            block_id=u.block_id,
            block_name=u.block_name,
            status=UnitSafety(u.status),
            note=u.note,
            muster_point=u.muster_point,
            by=u.responded_by,
            method=u.method,
            at=u.responded_at,
        )
        for u in sorted(units, key=lambda u: _ORDER.get(u.status, 2))
    ]
    return EmergencyBoardOut(
        **_emergency_out(e).model_dump(), rows=rows, totals=_totals(em_svc.totals(units))
    )


class EmergencyStartIn(BaseModel):
    kind: str | None = Field(default=None, max_length=20)
    scope: str = Field(default="all", max_length=10)
    block_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    message: str | None = Field(default=None, max_length=1000)


@router.post("/emergencies", status_code=status.HTTP_201_CREATED, summary="Acil durumu başlat")
async def start_emergency(
    body: EmergencyStartIn, ctx: Starter, current: CurrentUserDep, now: NowDep
) -> Written[EmergencyOut]:
    try:
        e, sent = await em_svc.start(
            ctx.session,
            kind=body.kind,
            scope=body.scope,
            block_ids=body.block_ids,
            message=body.message,
            by=current.user.full_name,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    count = len(await em_svc.rows(ctx.session, e.id))
    await ctx.session.commit()
    return Written(
        data=_emergency_out(e),
        message=f"Acil durum başlatıldı: {count} bölüm kapsamda, {sent} kişiye uygulama içi acil "
        "bildirim gitti (sessiz saat ve tercihleri aşar). SMS/push gönderimi sağlayıcı "
        "bağlanınca (K5).",
    )


@router.get("/emergencies/active", summary="Aktif acil durum ve pano (yoksa null)")
async def active_emergency(ctx: Board) -> EmergencyBoardOut | None:
    e = await em_svc.active(ctx.session)
    return await _board_out(ctx, e) if e else None


@router.get("/emergencies", summary="Acil durumlar (geçmiş; yeni üstte)")
async def emergencies(ctx: Board) -> list[EmergencyOut]:
    rows = await ctx.session.scalars(
        select(Emergency).order_by(Emergency.started_at.desc()).limit(100)
    )
    return [_emergency_out(e) for e in rows]


async def _emergency(ctx: SiteContext, emergency_id: uuid.UUID) -> Emergency:
    e = await em_svc.get(ctx.session, emergency_id)
    if e is None:
        raise NotFoundError("Acil durum bulunamadı.")
    return e


class MusterCheckIn(BaseModel):
    unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    muster_point: str | None = Field(default=None, max_length=120)


class MusterCheckOut(BaseModel):
    marked: int
    skipped_help: int = Field(description="yardım isteyen bölüm yoklamayla kapanmaz")


@router.post("/emergencies/{emergency_id}/muster-checks", summary="Listeden toplanma yoklaması")
async def muster_checks(
    emergency_id: uuid.UUID, body: MusterCheckIn, ctx: Board, current: CurrentUserDep, now: NowDep
) -> Written[MusterCheckOut]:
    e = await _emergency(ctx, emergency_id)
    try:
        marked, skipped = await em_svc.muster_check(
            ctx.session, e, unit_ids=body.unit_ids, muster_point=body.muster_point,
            by=current.user.full_name, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    message = f'{marked} bölüm "{body.muster_point}" toplanma alanında işaretlendi.'
    if skipped:
        message += f" Yardım isteyen {skipped} bölüm yoklamayla kapanmaz; ekip yönlendirilmeli."
    return Written(data=MusterCheckOut(marked=marked, skipped_help=skipped), message=message)


@router.post("/emergencies/{emergency_id}/end", summary="Acil durumu bitir (özet üretilir)")
async def end_emergency(
    emergency_id: uuid.UUID, ctx: Starter, current: CurrentUserDep, now: NowDep
) -> Written[EmergencyOut]:
    e = await _emergency(ctx, emergency_id)
    try:
        await em_svc.end(ctx.session, e, by=current.user.full_name, now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    t = _totals(e.summary or {})
    days = (await comm_svc.retention(ctx.session)).retention_days
    return Written(
        data=_emergency_out(e),
        message=f"Acil durum bitirildi. Özet: {t.safe} güvende, {t.muster} toplanma alanında, "
        f"{t.help} yardım, {t.none} yanıt yok. Ayrıntılı veriler {days} gün sonra silinir.",
    )


# --- Sakin --------------------------------------------------------------------------------


class MyUnitSafetyOut(BaseModel):
    status: UnitSafety
    note: str | None
    muster_point: str | None
    at: dt.datetime | None


class MyEmergencyUnit(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    status: MyUnitSafetyOut | None = Field(description="henüz yanıt yoksa null")


class ResidentEmergencyOut(BaseModel):
    id: uuid.UUID
    kind: EmergencyKind
    kind_label: str
    message: str
    started_at: dt.datetime
    units: list[MyEmergencyUnit]
    muster_points: list[str] = Field(description="acil bilgi kartındaki toplanma alanları")


@resident_router.get("/emergency", summary="Aktif acil durum (kapsamdaysam; yoksa null)")
async def my_emergency(ctx: Resident, today: TodayDep) -> ResidentEmergencyOut | None:
    e = await em_svc.active(ctx.session)
    if e is None or ctx.access.person_id is None:
        return None
    units = await em_svc.person_scope_units(ctx.session, e, ctx.access.person_id, today)
    if not units:
        return None
    points = [str(p["name"]) for p in (await comm_svc.card(ctx.session)).muster_points]
    return ResidentEmergencyOut(
        id=e.id,
        kind=EmergencyKind(e.kind),
        kind_label=EMERGENCY_LABELS[EmergencyKind(e.kind)],
        message=e.message,
        started_at=e.started_at,
        units=[
            MyEmergencyUnit(
                unit_id=u.unit_id,
                unit_name=u.unit_name,
                status=MyUnitSafetyOut(
                    status=UnitSafety(u.status),
                    note=u.note,
                    muster_point=u.muster_point,
                    at=u.responded_at,
                )
                if u.status != UnitSafety.NONE.value
                else None,
            )
            for u in units
        ],
        muster_points=points,
    )


class EmergencyStatusIn(BaseModel):
    status: str | None = Field(default=None, max_length=10, description="safe · help · muster")
    note: str | None = Field(default=None, max_length=400)
    muster_point: str | None = Field(default=None, max_length=120)


class EmergencyStatusOut(BaseModel):
    status: UnitSafety
    units: int


_STATUS_MESSAGES = {
    UnitSafety.HELP: "Yardım isteğiniz yönetime ve güvenliğe iletildi; ekip yönlendiriliyor.",
    UnitSafety.MUSTER: "Toplanma alanında olduğunuz kaydedildi.",
    UnitSafety.SAFE: "Güvende olduğunuz kaydedildi.",
}


@resident_router.post("/emergency/status", summary="Durumumu bildir (güvendeyim / yardım)")
async def my_emergency_status(
    body: EmergencyStatusIn, ctx: Resident, current: CurrentUserDep, now: NowDep
) -> Written[EmergencyStatusOut]:
    e = await em_svc.active(ctx.session, lock=True)
    if e is None or ctx.access.person_id is None:
        raise ApiError(HTTPStatus.CONFLICT, "not_active", "Aktif acil durum yok.")
    try:
        state, count = await em_svc.resident_status(
            ctx.session, e, person_id=ctx.access.person_id, status=body.status, note=body.note,
            muster_point=body.muster_point, by=current.user.full_name, now=now,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except PermissionError as exc:
        raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
    if state is UnitSafety.HELP:
        record(ctx.session, AuditAction.UPDATE, "emergencies", e.id, {"help_requested": True})
    await ctx.session.commit()
    return Written(
        data=EmergencyStatusOut(status=state, units=count), message=_STATUS_MESSAGES[state]
    )
