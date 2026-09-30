"""Modül kapısı — saf kurallar (docs/01-urun.md "Modüller", docs/10-demo-veri.md §3).

Bir modülün kullanılabilmesi için iki şart vardır: **sitenin planı o modüle izin vermeli** ve
**sitede açık olmalı.** Çekirdek (`finance`) her sitede açıktır ve kapatılamaz. Kapatılan
modülün verisi silinmez; modül yeniden açılınca kayıtlar yerindedir.
"""

from collections.abc import Iterable, Set
from dataclasses import dataclass
from enum import StrEnum


class ModuleKey(StrEnum):
    FINANCE = "finance"
    ANNOUNCEMENTS = "announcements"
    REQUESTS = "requests"
    VISITORS = "visitors"
    PACKAGES = "packages"
    DOCUMENTS = "documents"
    RESERVATIONS = "reservations"
    VALET = "valet"
    GENERAL_ASSEMBLY = "general-assembly"
    SURVEYS = "surveys"
    STAFF = "staff"
    PORTFOLIO = "portfolio"


CORE_MODULES: frozenset[ModuleKey] = frozenset({ModuleKey.FINANCE})

# Yeni sitede açık başlayanlar: çekirdek + planın izin verdiği bu üçü (docs/10 §3.2).
DEFAULT_ENABLED: frozenset[ModuleKey] = frozenset(
    {ModuleKey.ANNOUNCEMENTS, ModuleKey.REQUESTS, ModuleKey.DOCUMENTS}
)


class ModuleRuleError(Exception):
    """İş kuralı ihlali. `code` makine içindir, mesaj Türkçedir."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ModuleStatus:
    key: ModuleKey
    enabled: bool
    in_plan: bool

    @property
    def available(self) -> bool:
        return self.enabled and self.in_plan


def plan_allows(plan_modules: Iterable[str] | None) -> frozenset[ModuleKey]:
    """Planın izin verdiği modüller. Çekirdek her zaman dahildir.

    Planı olmayan site yalnız çekirdeği kullanır (kapalı başarısızlık); bilinmeyen anahtarlar
    yok sayılır.
    """
    known = {key.value for key in ModuleKey}
    allowed = {ModuleKey(value) for value in (plan_modules or ()) if value in known}
    return frozenset(allowed | CORE_MODULES)


def initial_state(plan_modules: Iterable[str] | None) -> dict[ModuleKey, bool]:
    """Yeni site kurulumunda her modül için başlangıç durumu (hepsi için bir satır)."""
    allowed = plan_allows(plan_modules)
    return {
        key: key in CORE_MODULES or (key in DEFAULT_ENABLED and key in allowed) for key in ModuleKey
    }


def check_toggle(key: ModuleKey, enable: bool, plan_modules: Iterable[str] | None) -> None:
    """Modül aç/kapa isteğini doğrular; kurala aykırıysa `ModuleRuleError`."""
    if key in CORE_MODULES and not enable:
        raise ModuleRuleError(
            "core_module_cannot_be_disabled",
            "Finans modülü çekirdek modüldür ve kapatılamaz.",
        )
    if enable and key not in plan_allows(plan_modules):
        raise ModuleRuleError(
            "module_not_in_plan",
            "Bu modül sitenin planında yok. Açmak için önce planı yükseltin.",
        )


def statuses(enabled: Set[ModuleKey], plan_modules: Iterable[str] | None) -> list[ModuleStatus]:
    """Her modülün durumu: açık mı, planda var mı (docs/06 §2.13 `GET …/modules`)."""
    allowed = plan_allows(plan_modules)
    return [
        ModuleStatus(key, enabled=key in enabled or key in CORE_MODULES, in_plan=key in allowed)
        for key in ModuleKey
    ]
