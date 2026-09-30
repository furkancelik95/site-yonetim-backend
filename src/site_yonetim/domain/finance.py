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
    OPEN = "open"
    CLOSED = "closed"  # kapalı döneme kayıt yazılamaz


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
    DRAFT = "draft"
    POSTED = "posted"
    REVERSED = "reversed"


class LedgerSource(StrEnum):
    CHARGE = "charge"
    PAYMENT = "payment"
    LATE_FEE = "late_fee"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"
    ADVANCE = "advance"
