"""Kapı işleri: kargo ve ziyaretçi — saf (docs/03 §9, docs/06 §2.12).

Teslim kodu yalnız sakine gösterilir; güvenlik kodu sakinden alır ve sistem doğrular. Kod
karşılaştırması sabit zamanlıdır.
"""

import hmac
from enum import StrEnum

from site_yonetim.domain.operations import OperationRuleError

PICKUP_CODE_MIN, PICKUP_CODE_MAX = 1000, 9999


class PackageStatus(StrEnum):
    WAITING = "waiting"
    DELIVERED = "delivered"
    RETURNED = "returned"


class VisitorKind(StrEnum):
    GUEST = "guest"
    CARGO = "cargo"
    SERVICE = "service"
    CONTRACTOR = "contractor"


class IncidentKind(StrEnum):
    THEFT = "theft"
    DAMAGE = "damage"
    NOISE = "noise"
    FIRE = "fire"
    WATER_LEAK = "water_leak"
    SUSPICIOUS = "suspicious"
    ACCIDENT = "accident"
    OTHER = "other"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class LostItemStatus(StrEnum):
    WAITING = "waiting"
    RETURNED = "returned"  # sahibine teslim
    DISPOSED = "disposed"  # bağış / imha (bekleme süresi ürün kararı; şimdilik elle)


class VisitorStatus(StrEnum):
    EXPECTED = "expected"
    ENTERED = "entered"
    EXITED = "exited"
    DENIED = "denied"


def check_delivery(status: PackageStatus, expected_code: str, given_code: str) -> None:
    if status is not PackageStatus.WAITING:
        raise OperationRuleError(
            "package_not_waiting",
            "Bu kargo zaten teslim edilmiş ya da iade edilmiş.",
            conflict=True,
        )
    if not hmac.compare_digest(expected_code.encode(), given_code.strip().encode()):
        raise OperationRuleError(
            "wrong_pickup_code", "Teslim kodu hatalı. Kodu sakinden yeniden isteyin.",
            field="pickup_code",
        )  # fmt: skip


def check_enter(status: VisitorStatus) -> None:
    if status is not VisitorStatus.EXPECTED:
        raise OperationRuleError(
            "visitor_not_expected", "Bu ziyaretçinin girişi zaten kaydedilmiş.", conflict=True
        )


def check_exit(status: VisitorStatus) -> None:
    if status is not VisitorStatus.ENTERED:
        raise OperationRuleError(
            "visitor_not_inside", "Çıkış için önce giriş kaydedilmeli.", conflict=True
        )
