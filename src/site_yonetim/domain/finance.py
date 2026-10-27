"""Finans değer kümeleri (docs/03 §5–§7). Veritabanında TEXT + CHECK olarak saklanır."""

from enum import StrEnum


class FinanceRuleError(Exception):
    """Finans iş kuralı ihlali: `conflict` → 409 (aynı dönem, zaten ters kayıt…), yoksa 422."""

    def __init__(
        self, code: str, message: str, *, field: str | None = None, conflict: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.conflict = conflict


class PeriodStatus(StrEnum):
    """Dönem kapanışı (FIN-13): Açık → Kapanışta → Kilitli; Kilitli → (istisnai) Süreli açık →
    Kilitli. Kapanışta ve kilitli dönemde finansal kayıt yazılamaz (`services/periods.py`)."""

    OPEN = "open"
    CLOSING = "closing"  # kontrol listesi geçti; yeni işlemler sonraki döneme
    LOCKED = "locked"  # hiçbir finansal kayıt eklenemez/değişmez
    TEMP_OPEN = "temp_open"  # onaylı, süreli açma — süre bitince kendiliğinden kilitlenir


class ExpenseCategoryKind(StrEnum):
    OPERATING = "operating"  # işletme — oturan öder
    CAPITAL_IMPROVEMENT = "capital_improvement"  # demirbaş/yatırım — malik öder


class PayerRule(StrEnum):
    """KMK m.22: kim öder."""

    OCCUPANT = "occupant"  # aidat, işletme gideri → aktif kiracı, yoksa malik
    OWNER = "owner"  # demirbaş, yatırım → her durumda malik


class AllocationKind(StrEnum):
    EQUAL = "equal"
    BY_LAND_SHARE = "by_land_share"
    BY_AREA = "by_area"
    BY_UNIT_TYPE_WEIGHT = "by_unit_type_weight"
    BY_CUSTOM_WEIGHT = "by_custom_weight"
    FIXED_PER_UNIT = "fixed_per_unit"
    BY_METER_CONSUMPTION = "by_meter_consumption"
    COMPOSITE = "composite"


# Bileşik kuralın parçası olabilecek türler: havuzu paylaştıranlar.
COMPONENT_KINDS = frozenset(AllocationKind) - {
    AllocationKind.COMPOSITE,
    AllocationKind.FIXED_PER_UNIT,
}


class AreaBasis(StrEnum):
    GROSS = "gross"
    NET = "net"


class BudgetStatus(StrEnum):
    DRAFT = "draft"
    NOTIFIED = "notified"  # tebliğ edildi — 7 gün itiraz süresi
    FINALIZED = "finalized"  # kesinleşti — değiştirilemez (İİK m.68 belgesi)
    SUPERSEDED = "superseded"  # yenisi kesinleşti


class Frequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    ONE_TIME = "one_time"


class ScopeKind(StrEnum):
    WHOLE_SITE = "whole_site"
    BLOCKS = "blocks"
    UNIT_TYPES = "unit_types"
    USAGE = "usage"


class ChargeRunStatus(StrEnum):
    """FIN-05 dönem durum modeli: Taslak → Onay bekliyor → Onaylandı → Kesinleşti (`posted`).
    Kesinleşmiş koşu yalnız ters kayıtla düzeltilir (`reversed`)."""

    DRAFT = "draft"  # taslak: önizleme anlık görüntüsü saklanır, deftere yazılmaz
    APPROVAL_PENDING = "approval_pending"  # "Tahakkuk" onay kuralında
    APPROVED = "approved"  # onaylandı; tahakkuk tarihinde kesinleşir
    REJECTED = "rejected"  # onay reddedildi / geri çekildi — taslak düzeltilip yeniden gönderilir
    CANCELLED = "cancelled"  # taslaktan vazgeçildi
    POSTED = "posted"
    REVERSED = "reversed"


OPEN_DRAFT_STATUSES = frozenset(
    {ChargeRunStatus.DRAFT, ChargeRunStatus.APPROVAL_PENDING, ChargeRunStatus.APPROVED}
)


class ScheduleRunStatus(StrEnum):
    POSTED = "posted"  # tahakkuk kesildi
    DRAFTED = "drafted"  # taslak hazırlandı ve "Tahakkuk" onayına gönderildi (FIN-05)
    SKIPPED = "skipped"  # dönem zaten kesilmiş / proje yok / önizlemede uyarı
    FAILED = "failed"  # beklenmeyen hata — ertesi gün yeniden denenir


class LedgerSource(StrEnum):
    CHARGE = "charge"
    PAYMENT = "payment"
    LATE_FEE = "late_fee"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"
    ADVANCE = "advance"
    OPENING = "opening"  # devir bakiye: sisteme geçişte önceki yönetimden kalan (hesap başına bir)
    REFUND = "refund"  # alacaklı bakiyenin sakine geri ödenmesi — açık borç sayılmaz
    AD_HOC_CHARGE = "ad_hoc_charge"  # dönem dışı ücret (FIN-06)
    PAYMENT_PLAN = "payment_plan"  # ödeme planı: yapılandırma alacağı + taksit borçları (FIN-12)
    SERVICE = "service"  # ücretli hizmet (HIZ-04): sakinin onayladığı teklif
    DEPOSIT = "deposit"  # tadilat depozitosu (HIZ-05); iadesi ters kayıt
