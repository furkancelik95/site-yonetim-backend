"""Platform destek — saf kurallar (Yazılım Dokümanı PYN-04, PYN-06, 19.2–19.3, PLT-12;
servis istekleri 82–84; PM kararı K12).

- **Destek bileti (PYN-06):** yönetim rolleri açar; ekran bağlamı otomatik eklenir. Durumlar:
  açık → yanıtlandı → müşteri yanıtı bekleniyor → çözüldü → kapandı. İlk yanıt ve çözüm süresi
  paket SLA'sına göre ölçülür. Paket SLA sınırları ortak kararı bekler (soru 1.9, K13/K19 "pilot
  sonrası"); o zamana kadar her destek seviyesinde servis isteğindeki varsayılan hedefler
  (takvim saati) geçerlidir — `SLA_TARGETS` tek yerdir.
- Kritik öncelik yalnız paket destek seviyesi 7/24 içeriyorsa nöbet hattına düşer
  (Ürün Dokümanı 3.2: Profesyonel → kritik konular 7/24, Kurumsal → tam 7/24).
- **Destek oturumu (PYN-04, K12):** müşteri onayı olmadan açılmaz; süre 1–24 saat; salt okunur.
  Bir tesiste aynı anda tek bekleyen/aktif oturum.
- **Sakin olarak görüntüle (PLT-12):** en fazla 30 dakika, gerekçe zorunlu, salt okunur.

Zaman hesabı `datetime` aritmetiğidir; saati okumaz (`now` çağıran verir).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from site_yonetim.domain.access import Permission


class TicketCategory(StrEnum):
    LOGIN = "login"
    PAYMENT = "payment"
    FINANCE = "finance"
    IMPORT = "import"
    SECURITY = "security"
    OTHER = "other"


class TicketPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"


class TicketStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class MessageSide(StrEnum):
    CUSTOMER = "customer"
    SUPPORT = "support"


class SupportTier(StrEnum):
    """Paket destek seviyesi (Ürün Dokümanı 3.2)."""

    BUSINESS_HOURS = "business_hours"  # Başlangıç: mesai içi
    CRITICAL_24_7 = "critical_24_7"  # Profesyonel: kritik konular 7/24
    FULL_24_7 = "full_24_7"  # Kurumsal: tam 7/24


class SupportSessionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"  # süre doldu ya da tesis yöneticisi bitirdi


class ViewAsStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"  # görüntüleyen çıktı
    EXPIRED = "expired"  # süre doldu


class ArticleAudience(StrEnum):
    STAFF = "staff"
    RESIDENT = "resident"


# --- Bilet ve SLA ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlaTarget:
    first_response_hours: int
    resolve_hours: int


_DEFAULT_TARGETS: dict[TicketPriority, SlaTarget] = {
    TicketPriority.CRITICAL: SlaTarget(1, 8),
    TicketPriority.HIGH: SlaTarget(4, 24),
    TicketPriority.NORMAL: SlaTarget(8, 72),
}
# Paket SLA'sı (soru 1.9 açık): şimdilik her seviyede aynı varsayılan hedefler.
SLA_TARGETS: dict[SupportTier, dict[TicketPriority, SlaTarget]] = {
    tier: dict(_DEFAULT_TARGETS) for tier in SupportTier
}
ON_CALL_TIERS = frozenset({SupportTier.CRITICAL_24_7, SupportTier.FULL_24_7})

# Yazışmaya müşteri ve destek tarafında izin verilen durumlar
SUPPORT_REPLY_STATUSES = frozenset(
    {TicketStatus.ANSWERED, TicketStatus.WAITING_CUSTOMER, TicketStatus.RESOLVED}
)
TITLE_MIN, TITLE_MAX = 5, 120
BODY_MIN, BODY_MAX = 10, 4000
MESSAGE_MIN, MESSAGE_MAX = 2, 4000
CONTEXT_PATH_MAX, CONTEXT_VERSION_MAX, CONTEXT_AGENT_MAX = 200, 40, 200
TICKET_PREFIX = "DST-"

# Bileti açan / görebilen: yönetim rolleri (servis isteği 82 "Yetki")
TICKET_PERMISSIONS = frozenset(
    {Permission.MEMBERS_MANAGE, Permission.FINANCE_READ, Permission.MODULES_MANAGE}
)


def ticket_number(number: int) -> str:
    return f"{TICKET_PREFIX}{number:04d}"


@dataclass(frozen=True, slots=True)
class SlaDue:
    first_due: datetime
    resolve_due: datetime


def sla_due(tier: SupportTier, priority: TicketPriority, created_at: datetime) -> SlaDue:
    target = SLA_TARGETS[tier][priority]
    return SlaDue(
        created_at + timedelta(hours=target.first_response_hours),
        created_at + timedelta(hours=target.resolve_hours),
    )


def routes_to_on_call(tier: SupportTier, priority: TicketPriority) -> bool:
    """Kritik öncelik yalnız paket 7/24 destek içeriyorsa nöbet hattına düşer (19.3)."""
    return priority is TicketPriority.CRITICAL and tier in ON_CALL_TIERS


def first_breached(first_due: datetime, first_response_at: datetime | None, now: datetime) -> bool:
    return (first_response_at or now) > first_due


def resolve_breached(resolve_due: datetime, resolved_at: datetime | None, now: datetime) -> bool:
    return (resolved_at or now) > resolve_due


class TicketRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


CLOSED_MESSAGE = "Bilet kapandı; yeni bilet açın."


def after_customer_message(status: TicketStatus) -> TicketStatus:
    """Müşteri yazınca bilet yeniden açılır; kapalı bilete yazılmaz."""
    if status is TicketStatus.CLOSED:
        raise TicketRuleError("closed", CLOSED_MESSAGE)
    return TicketStatus.OPEN


def after_support_message(status: TicketStatus, requested: TicketStatus) -> TicketStatus:
    if status is TicketStatus.CLOSED:
        raise TicketRuleError("closed", CLOSED_MESSAGE)
    if requested not in SUPPORT_REPLY_STATUSES:  # pragma: no cover - şema zaten süzer
        raise TicketRuleError("invalid_status", "Geçersiz durum.")
    return requested


def close_by_customer(status: TicketStatus) -> TicketStatus:
    if status is not TicketStatus.RESOLVED:
        raise TicketRuleError("invalid_status", "Bilet çözüldü olarak işaretlenmedi.")
    return TicketStatus.CLOSED


# --- Bilgi bankası -------------------------------------------------------------------

ARTICLE_TITLE_MIN, ARTICLE_TITLE_MAX = 5, 120
ARTICLE_BODY_MIN, ARTICLE_BODY_MAX = 30, 20000
SCREENS_MAX, SCREEN_MAX = 20, 80


def clean_screens(screens: list[str]) -> list[str]:
    """Ekran eşleşmesi yol önekiyle: baştaki "/" atılır, boşlar ve tekrarlar düşer."""
    out: list[str] = []
    for raw in screens:
        value = raw.strip().strip("/")[:SCREEN_MAX]
        if value and value not in out:
            out.append(value)
    return out[:SCREENS_MAX]


def screen_matches(screens: list[str], screen: str) -> bool:
    """`satin-alma` makalesi `satin-alma/talepler` ekranında da görünür."""
    target = screen.strip().strip("/")
    return any(target == s or target.startswith(f"{s}/") for s in screens)


# --- Destek oturumu ve sakin olarak görüntüle ------------------------------------------

SESSION_HOURS_MIN, SESSION_HOURS_MAX = 1, 24
REASON_MIN, REASON_MAX = 10, 300
VIEW_AS_MINUTES = 30
VIEW_AS_REASON_MIN = 10
OPEN_SESSION_STATUSES = frozenset({SupportSessionStatus.PENDING, SupportSessionStatus.ACTIVE})

# Destek oturumunda platform ekibinin sitedeki izinleri: yalnız okuma. İletişim bilgisi
# (people.contact) ve gizli belgeler verilmez — KVKK: erişim amaçla sınırlı, kişisel veri maskeli
# kalır. "Sakin olarak görüntüle" oturum içinde kullanılabilir (PYN-04 adım 5, PLT-12 roller).
SUPPORT_SESSION_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.FINANCE_READ,
        Permission.FINANCE_CASH_READ,
        Permission.FINANCE_REPORTS_READ,
        Permission.UNITS_READ,
        Permission.PEOPLE_READ,
        Permission.REQUESTS_READ,
        Permission.ANNOUNCEMENTS_READ,
        Permission.EXPENSES_READ,
        Permission.AUDIT_READ,
        Permission.MEETINGS_READ,
        Permission.INVENTORY_READ,
        Permission.APPROVAL_RULES_READ,
        Permission.DOCUMENTS_READ,
        Permission.SUPPORT_VIEW_AS_RESIDENT,
    }
)
VIEW_AS_ROLE_KEY = "view-as"
VIEW_AS_ROLE_NAME = "Sakin olarak görüntüleme (salt okunur)"


def session_effective_status(
    status: SupportSessionStatus, ends_at: datetime | None, now: datetime
) -> SupportSessionStatus:
    """Süresi dolan aktif oturum, zamanlanmış iş kapatmadan da "sona erdi" sayılır."""
    if status is SupportSessionStatus.ACTIVE and ends_at is not None and ends_at <= now:
        return SupportSessionStatus.EXPIRED
    return status


def view_as_effective_status(
    status: ViewAsStatus, ends_at: datetime, now: datetime
) -> ViewAsStatus:
    if status is ViewAsStatus.ACTIVE and ends_at <= now:
        return ViewAsStatus.EXPIRED
    return status
