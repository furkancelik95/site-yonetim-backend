"""İletişim ve acil durum — saf kurallar (Yazılım Dokümanı Bölüm 16, ILT-01..06; RAP-02).

Burada yalnız hesap var (veritabanı yok): sessiz saat kaydırması, SMS parça sayısı, şablon
değişken doğrulaması, acil bilgi kartı doğrulaması, şeffaflık raporu dönem kuralları.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

# --- Okuma onayı ve zamanlama (ILT-01, 16.2–16.3) ----------------------------------------

ACK_HOURS_MIN, ACK_HOURS_MAX = 1, 720
ACK_HOURS_DEFAULT = 48
DEFAULT_QUIET_FROM, DEFAULT_QUIET_TO = time(22, 0), time(8, 0)  # 16.2: site ayarı varsayılanı


def in_quiet(moment: time, quiet_from: time, quiet_to: time) -> bool:
    """Aralık gece yarısını geçebilir (22:00–08:00). Başlangıç dahil, bitiş hariç."""
    if quiet_from == quiet_to:
        return False
    if quiet_from > quiet_to:
        return moment >= quiet_from or moment < quiet_to
    return quiet_from <= moment < quiet_to


def shift_out_of_quiet(local: datetime, quiet_from: time, quiet_to: time) -> tuple[datetime, bool]:
    """Yerel saatte sessiz saate düşen an → sessiz saat bitişi (aynı ya da ertesi gün)."""
    if not in_quiet(local.timetz().replace(tzinfo=None), quiet_from, quiet_to):
        return local, False
    target = local.replace(hour=quiet_to.hour, minute=quiet_to.minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target, True


# --- SMS parça sayısı (ILT-02) ----------------------------------------------------------

_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€"
)
SMS_MAX_SEGMENTS = 3


@dataclass(frozen=True, slots=True)
class SmsCount:
    encoding: str  # "GSM-7" | "Unicode"
    chars: int
    segments: int


def sms_count(text: str) -> SmsCount:
    """GSM-7: 160 (çok parçada 153); Türkçe/Unicode karakter varsa 70 (67)."""
    gsm = all(ch in _GSM7 for ch in text)
    single, multi = (160, 153) if gsm else (70, 67)
    n = len(text)
    segments = 0 if n == 0 else 1 if n <= single else -(-n // multi)
    return SmsCount("GSM-7" if gsm else "Unicode", n, segments)


# --- Şablonlar (ILT-02) -----------------------------------------------------------------


class TemplateChannel(StrEnum):
    PUSH = "push"
    SMS = "sms"
    EMAIL = "email"
    ANNOUNCEMENT = "announcement"  # hazır duyuru metni (başlık + metin)


TEMPLATE_LANGS = ("tr", "en")
TEMPLATE_BODY_MAX, TEMPLATE_SUBJECT_MAX = 2000, 120
_VARIABLE = re.compile(r"\{\{\s*([a-z_.]+)\s*\}\}")


@dataclass(frozen=True, slots=True)
class Variant:
    lang: str
    subject: str | None
    body: str


@dataclass(frozen=True, slots=True)
class TemplateDef:
    code: str
    name: str
    channel: TemplateChannel
    variables: tuple[str, ...]
    required: tuple[str, ...]
    variants: tuple[Variant, ...]


def _v(lang: str, body: str, subject: str | None = None) -> Variant:
    return Variant(lang, subject, body)


# Sistem şablonları: site kopyalayıp özelleştirir (sürüm 1 = sistem metni).
SYSTEM_TEMPLATES: tuple[TemplateDef, ...] = (
    TemplateDef(
        "dues_reminder", "Aidat hatırlatma", TemplateChannel.SMS,
        ("sakin.ad", "bolum", "tutar", "vade", "site.ad"), ("tutar", "vade"),
        (
            _v("tr", "Sayın {{sakin.ad}}, {{bolum}} için {{tutar}} tutarındaki aidatın vadesi "
               "{{vade}}. {{site.ad}} Yönetimi"),
            _v("en", "Dear {{sakin.ad}}, the dues of {{tutar}} for {{bolum}} are due on {{vade}}. "
               "{{site.ad}} Management"),
        ),
    ),
    TemplateDef(
        "payment_received", "Tahsilat alındı", TemplateChannel.PUSH,
        ("sakin.ad", "tutar", "makbuz.no"), ("tutar",),
        (
            _v("tr", "{{tutar}} ödemeniz alındı. Makbuz: {{makbuz.no}}"),
            _v("en", "Your payment of {{tutar}} was received. Receipt: {{makbuz.no}}"),
        ),
    ),
    TemplateDef(
        "request_status", "Talep durumu", TemplateChannel.PUSH,
        ("talep.no", "talep.konu", "durum"), ("talep.no", "durum"),
        (
            _v("tr", "#{{talep.no}} {{talep.konu}}: {{durum}}"),
            _v("en", "#{{talep.no}} {{talep.konu}}: {{durum}}"),
        ),
    ),
    TemplateDef(
        "visitor_arrived", "Ziyaretçi geldi", TemplateChannel.PUSH,
        ("ziyaretci.ad", "kapi"), ("ziyaretci.ad",),
        (
            _v("tr", "{{ziyaretci.ad}} {{kapi}} kapısında, onayınızı bekliyor."),
            _v("en", "{{ziyaretci.ad}} is at the {{kapi}} gate, waiting for your approval."),
        ),
    ),
    TemplateDef(
        "package_arrived", "Kargo geldi", TemplateChannel.SMS,
        ("firma", "teslim.kodu"), ("teslim.kodu",),
        (
            _v("tr", "{{firma}} kargonuz güvenlikte. Teslim kodu: {{teslim.kodu}}"),
            _v("en", "Your {{firma}} parcel is at security. Pickup code: {{teslim.kodu}}"),
        ),
    ),
    TemplateDef(
        "ack_reminder", "Okuma onayı yedeği (SMS)", TemplateChannel.SMS,
        ("duyuru.baslik", "link"), ("duyuru.baslik", "link"),
        (
            _v("tr", "Okuma onayı bekleyen duyuru: {{duyuru.baslik}} {{link}}"),
            _v("en", "Announcement awaiting acknowledgement: {{duyuru.baslik}} {{link}}"),
        ),
    ),
    TemplateDef(
        "emergency", "Acil durum", TemplateChannel.SMS,
        ("tur", "mesaj", "link"), ("mesaj",),
        (
            _v("tr", "ACİL DURUM ({{tur}}): {{mesaj}} Güvende misiniz? {{link}}"),
            _v("en", "EMERGENCY ({{tur}}): {{mesaj}} Are you safe? {{link}}"),
        ),
    ),
    TemplateDef(
        "ann_water_outage", "Duyuru: su kesintisi", TemplateChannel.ANNOUNCEMENT,
        ("tarih", "saat", "neden"), ("tarih",),
        (
            _v("tr", "{{tarih}} günü {{saat}} saatleri arasında {{neden}} nedeniyle sularımız "
               "kesilecektir. Anlayışınız için teşekkür ederiz.", "Planlı su kesintisi"),
            _v("en", "Water will be cut on {{tarih}} between {{saat}} due to {{neden}}. "
               "Thank you for your understanding.", "Planned water outage"),
        ),
    ),
    TemplateDef(
        "ann_general_assembly", "Duyuru: genel kurul çağrısı", TemplateChannel.ANNOUNCEMENT,
        ("tarih", "saat", "yer", "gundem"), ("tarih", "yer"),
        (
            _v("tr", "Kat malikleri genel kurulu {{tarih}} {{saat}}'te {{yer}}'de toplanacaktır. "
               "Gündem: {{gundem}}", "Genel kurul çağrısı"),
        ),
    ),
)  # fmt: skip
SYSTEM_BY_CODE = {t.code: t for t in SYSTEM_TEMPLATES}


def variables_in(text: str) -> list[str]:
    return [m.group(1) for m in _VARIABLE.finditer(text)]


def check_variants(defn: TemplateDef, variants: list[Variant]) -> dict[str, str]:
    """Türkçe zorunlu; her dilde zorunlu değişken bulunmalı, tanımsız değişken olmamalı;
    SMS en fazla 3 parça. Hata anahtarı `variants` ya da `variants.{lang}`."""
    errors: dict[str, str] = {}
    langs = [v.lang for v in variants]
    if "tr" not in langs:
        errors["variants"] = "Türkçe metin zorunlu."
    elif len(set(langs)) != len(langs) or any(lang not in TEMPLATE_LANGS for lang in langs):
        errors["variants"] = "Her dil bir kez yazılır (tr, en)."
    for v in variants:
        key = f"variants.{v.lang}"
        used = variables_in(v.body) + variables_in(v.subject or "")
        missing = [x for x in defn.required if x not in used]
        unknown = sorted({x for x in used if x not in defn.variables})
        if not v.body.strip():
            errors[key] = "Metin boş olamaz."
        elif len(v.body) > TEMPLATE_BODY_MAX or len(v.subject or "") > TEMPLATE_SUBJECT_MAX:
            errors[key] = "Metin çok uzun."
        elif missing:
            errors[key] = "Zorunlu değişken eksik: " + ", ".join(f"{{{{{x}}}}}" for x in missing)
        elif unknown:
            errors[key] = "Tanımsız değişken: " + ", ".join(f"{{{{{x}}}}}" for x in unknown)
        elif defn.channel is TemplateChannel.SMS and sms_count(v.body).segments > SMS_MAX_SEGMENTS:
            errors[key] = "SMS en fazla 3 parça olabilir."
        elif defn.channel is TemplateChannel.ANNOUNCEMENT and not (v.subject or "").strip():
            errors[key] = "Duyuru şablonunda başlık zorunlu."
    return errors


# --- Acil durum (ILT-04, ILT-06) ----------------------------------------------------------


class EmergencyKind(StrEnum):
    EARTHQUAKE = "earthquake"
    FIRE = "fire"
    FLOOD = "flood"
    GAS = "gas"
    OTHER = "other"


EMERGENCY_LABELS = {
    EmergencyKind.EARTHQUAKE: "Deprem",
    EmergencyKind.FIRE: "Yangın",
    EmergencyKind.FLOOD: "Su baskını",
    EmergencyKind.GAS: "Gaz kaçağı",
    EmergencyKind.OTHER: "Acil durum",
}


class EmergencyScope(StrEnum):
    ALL = "all"
    BLOCKS = "blocks"


class UnitSafety(StrEnum):
    NONE = "none"  # yanıt yok
    SAFE = "safe"  # güvende (uygulamadan)
    MUSTER = "muster"  # toplanma alanında
    HELP = "help"  # yardım istiyor


class SafetyMethod(StrEnum):
    APP = "app"  # sakin beyanı
    LIST = "list"  # personel listeden işaretledi


RETENTION_DAYS_DEFAULT = 30  # 16.4 [ÖNERİ, A-04] — K8: doküman varsayılanı
RETENTION_DAYS_MIN, RETENTION_DAYS_MAX = 1, 365
MESSAGE_MIN, MESSAGE_MAX = 5, 300
NOTE_MAX = 200


def purge_on(ended: date, retention_days: int) -> date:
    return ended + timedelta(days=retention_days)


# --- Acil bilgi kartı (ILT-06, 16.4) --------------------------------------------------------

INSTRUCTION_KEYS = ("earthquake", "fire", "flood", "gas")
DEFAULT_INSTRUCTIONS = {
    "earthquake": "Çök–kapan–tutun. Sarsıntı bitince merdivenle, asansör kullanmadan toplanma "
    "alanına gidin.",
    "fire": "Asansör kullanmayın; yangın merdiveninden çıkın, kapıları kapatın, 112'yi arayın.",
    "flood": "Elektriği kapatın, bodrum ve otoparktan uzaklaşın, üst katlara çıkın.",
    "gas": "Elektrik düğmelerine dokunmayın, pencereleri açın, binayı terk edip 187'yi arayın.",
}
DEFAULT_PHONES = ({"label": "Acil çağrı", "number": "112"},)
MUSTER_POINTS_MAX, PHONES_MAX = 20, 20
MAP_MAX_CHARS = 400_000  # küçültülmüş görsel (data URL); belge deposuna taşınabilir
_MAP_PREFIX = re.compile(r"^data:image/(png|jpeg|webp);base64,")
_MAGIC = {"png": b"\x89PNG", "jpeg": b"\xff\xd8\xff", "webp": b"RIFF"}
_PHONE = re.compile(r"^\+?\d{3,13}$")


def clean_phone(value: str) -> str:
    return re.sub(r"[^\d+]", "", value)


def valid_phone(value: str) -> bool:
    return bool(_PHONE.fullmatch(value))


def valid_map(value: str) -> bool:
    """Yalnız PNG/JPEG/WEBP data URL (SVG değil: betik taşıyabilir); içerik imzası denetlenir."""
    match = _MAP_PREFIX.match(value)
    if match is None or len(value) > MAP_MAX_CHARS:
        return False
    try:
        head = base64.b64decode(value[match.end() : match.end() + 32], validate=True)
    except binascii.Error, ValueError:
        return False
    return head.startswith(_MAGIC[match.group(1)])


# --- Şeffaflık raporu (RAP-02) ------------------------------------------------------------


class TransparencyStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"  # onay bekliyor
    PUBLISHED = "published"
    REJECTED = "rejected"


def check_period(year: int, month: int, today: date) -> str | None:
    """Rapor yalnız başlamış bir ay için hazırlanır (gelecek ay olmaz)."""
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return "Geçerli bir dönem seçin."
    if (year, month) > (today.year, today.month):
        return "Gelecek bir dönem için rapor hazırlanamaz."
    return None
