"""Genel kurul — saf iş kuralları (Yazılım Dokümanı §17, TOP-01..04; servis istekleri 75–77).

Nisap ve çoğunluk **kişi sayısı ve arsa payı** ile ayrı ayrı hesaplanır; karar türü hangisini ya da
ikisini birden istiyorsa ona göre sonuçlanır (TOP-03 hesap kuralları). Hesap tam kesirle
(`Fraction`) yapılır: arsa payları farklı paydalı olabilir, kayan nokta sınırda yanlış sonuç verir.

Hukuki teyit bekleyen her kural **parametredir** (site ayarı) ve varsayılanı dokümandan / servis
isteğinden gelir (docs/12 — avukat soruları):
- karar türlerinin oranları ve tabanı (katılanlar / tüm malikler), çekimser oyun tabanda sayılması;
- toplantı nisabı (KMK m.30: sayı ve arsa payı yarıdan fazla) ve ikinci toplantıda aranan nisap
  (varsayılan: aranmaz);
- kişi sayımı: hisseli bölüm tek kişi, aynı malik(ler)in birden çok bölümü tek kişi (varsayılan)
  ya da her bölüm bir kişi;
- kişi başı azami vekâlet (varsayılan 2), çağrı süresi (15 gün) ve ikinci toplantı aralığı (7 gün);
- uzaktan oy ve dijital vekâlet: varsayılan KAPALI; SMS doğrulaması (P16c) ve hukuki dayanak
  belgesi olmadan açılamaz.
"""

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Any

from site_yonetim.domain.text import tr_lower


class AssemblyFormat(StrEnum):
    PHYSICAL = "physical"  # fiziki toplantı, salonda oylama
    HYBRID = "hybrid"  # fiziki + uzaktan
    PREVOTE = "prevote"  # ön oylama penceresi + toplantıda kesinleşme


class DecisionBase(StrEnum):
    PRESENT = "present"  # katılanlar (vekille temsil edilen dahil)
    ALL = "all"  # tüm malikler


class Ballot(StrEnum):
    FOR = "for"
    AGAINST = "against"
    ABSTAIN = "abstain"


class AttendanceMode(StrEnum):
    SELF = "self"
    PROXY = "proxy"


class VoteStatus(StrEnum):
    NONE = "none"
    OPEN = "open"
    CLOSED = "closed"


class PersonRule(StrEnum):
    OWNER_GROUP = "owner_group"  # bölümün malik kümesi bir kişi (hisseli tek, çok bölümlü tek)
    PER_UNIT = "per_unit"  # her bölüm bir kişi


class BallotChannel(StrEnum):
    HALL = "hall"  # salonda divanın girdiği oy
    APP = "app"  # uygulamadan (SMS kodlu) — B18, varsayılan kapalı


FORMAT_LABELS: dict[AssemblyFormat, str] = {
    AssemblyFormat.PHYSICAL: "Fiziki toplantı, salonda oylama",
    AssemblyFormat.HYBRID: "Hibrit (fiziki + uzaktan)",
    AssemblyFormat.PREVOTE: "Ön oylama penceresi + toplantıda kesinleşme",
}
BALLOT_LABELS: dict[Ballot, str] = {
    Ballot.FOR: "Kabul",
    Ballot.AGAINST: "Ret",
    Ballot.ABSTAIN: "Çekimser",
}

# Uzaktan oy ve dijital vekâlet her oyda SMS kodu ister (§17.2). SMS doğrulama altyapısı (P16c)
# bu sürümde yok: bayraklar açılamaz. P16c birleşince `True` yapılır ve kod doğrulaması bağlanır.
SMS_VERIFICATION_AVAILABLE = False

PROXY_LIMIT_DEFAULT, PROXY_LIMIT_MIN, PROXY_LIMIT_MAX = 2, 1, 20
NOTICE_DAYS_DEFAULT = 15  # KMK m.29 — mevzuat teyidine bağlı
SECOND_CALL_DAYS_DEFAULT = 7  # "ikinci toplantı bir hafta sonra" — mevzuat teyidine bağlı
DAYS_MAX = 90
ACK_HOURS_DEFAULT, ACK_HOURS_MIN, ACK_HOURS_MAX = 72, 1, 720
DECISION_TYPES_MAX = 12
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
NAME_MIN, NAME_MAX = 3, 120
RATIO_DEN_MAX = 100
PROXY_NAME_MIN, PROXY_NAME_MAX = 3, 120
NOTE_MAX, DECISION_MAX, OPENING_MAX = 4000, 2000, 4000
INVITATION_TITLE_MIN, INVITATION_TITLE_MAX = 3, 120
INVITATION_BODY_MIN, INVITATION_BODY_MAX = 10, 10_000


class AssemblyError(Exception):
    """İş kuralı ihlali: `status` (409/422), `code`, kullanıcıya dönük mesaj, alan hataları."""

    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


def conflict(code: str, message: str) -> AssemblyError:
    return AssemblyError(409, code, message)


# --- Eşik ve karar türü -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Threshold:
    """`num/den` oranı; `strict` → "fazlası" (>), değilse "en az" (≥). `person`/`share` hangi
    ölçütün arandığı."""

    num: int
    den: int
    strict: bool
    person: bool
    share: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "num": self.num,
            "den": self.den,
            "strict": self.strict,
            "person": self.person,
            "share": self.share,
        }


@dataclass(frozen=True, slots=True)
class DecisionType:
    code: str
    name: str
    base: DecisionBase
    num: int
    den: int
    strict: bool
    person: bool
    share: bool
    # Çekimser oy tabanda kalır mı (yalnız taban "katılanlar" ise anlamlı). True: çekimser "kabul"
    # sayılmaz ve tabanda kalır — fiilen ret etkisi (servis isteği 75 varsayılanı).
    abstain_in_base: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "base": self.base.value,
            "num": self.num,
            "den": self.den,
            "strict": self.strict,
            "person": self.person,
            "share": self.share,
            "abstain_in_base": self.abstain_in_base,
        }


DEFAULT_DECISION_TYPES: tuple[DecisionType, ...] = (
    DecisionType(
        "ordinary",
        "Oy çokluğu (katılanların sayı ve arsa payı çoğunluğu)",
        DecisionBase.PRESENT,
        1,
        2,
        True,
        True,
        True,
    ),
    DecisionType(
        "all_majority",
        "Tüm maliklerin sayı ve arsa payı çoğunluğu",
        DecisionBase.ALL,
        1,
        2,
        True,
        True,
        True,
    ),
    DecisionType(
        "four_fifths",
        "Tüm maliklerin 4/5'i (sayı ve arsa payı)",
        DecisionBase.ALL,
        4,
        5,
        False,
        True,
        True,
    ),
    DecisionType(
        "unanimous", "Oy birliği (tüm malikler)", DecisionBase.ALL, 1, 1, False, True, False
    ),
)
DEFAULT_QUORUM = Threshold(1, 2, True, True, True)  # KMK m.30: sayı ve arsa payı yarıdan fazla


def passes(value: Fraction, base: Fraction, num: int, den: int, *, strict: bool) -> bool:
    """`value / base` oranı `num/den`'i geçiyor mu (`strict`) ya da en az ona eşit mi.
    Taban sıfırsa sağlanmaz (kimse yokken "yarıdan fazla" olmaz)."""
    if base <= 0:
        return False
    return value * den > base * num if strict else value * den >= base * num


# --- Malik listesi ve kişi sayımı ----------------------------------------------------


def person_key(unit_id: uuid.UUID, owner_ids: Iterable[uuid.UUID], rule: PersonRule) -> str:
    """Kişi sayımında bölümün ait olduğu "kişi". Ad değil kişi kimliği kullanılır."""
    owners = sorted({str(o) for o in owner_ids})
    if rule is PersonRule.PER_UNIT or not owners:
        return f"unit:{unit_id}"
    return "+".join(owners)


@dataclass(frozen=True, slots=True)
class RosterUnit:
    unit_id: uuid.UUID
    key: str  # kişi anahtarı
    share: Fraction  # arsa payı (pay/payda); payı girilmemiş bölüm 0


def total_share(roster: Sequence[RosterUnit]) -> Fraction:
    return sum((u.share for u in roster), Fraction(0))


@dataclass(frozen=True, slots=True)
class QuorumResult:
    persons_present: int
    persons_total: int
    share_present: Fraction
    share_total: Fraction
    person_ok: bool
    share_ok: bool
    required: bool  # ikinci toplantıda nisap aranmıyorsa False
    ok: bool


def quorum(
    roster: Sequence[RosterUnit],
    present: set[uuid.UUID],
    rule: Threshold,
    *,
    second_call: bool = False,
    second_call_rule: Threshold | None = None,
) -> QuorumResult:
    """Toplantı nisabı. İkinci toplantıda `second_call_rule` uygulanır; `None` → aranmaz."""
    attending = [u for u in roster if u.unit_id in present]
    persons_total = len({u.key for u in roster})
    persons_present = len({u.key for u in attending})
    share_total = total_share(roster)
    share_present = total_share(attending)
    active = second_call_rule if second_call else rule
    if active is None:
        active, required = rule, False
    else:
        required = True
    person_ok = passes(
        Fraction(persons_present),
        Fraction(persons_total),
        active.num,
        active.den,
        strict=active.strict,
    )
    share_ok = passes(share_present, share_total, active.num, active.den, strict=active.strict)
    ok = not required or (
        (not active.person or person_ok)
        and (not active.share or share_ok)
        and (active.person or active.share)
    )
    return QuorumResult(
        persons_present,
        persons_total,
        share_present,
        share_total,
        person_ok,
        share_ok,
        required,
        ok,
    )


# --- Oylama ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonCounts:
    for_: int
    against: int
    abstain: int
    base: int


@dataclass(frozen=True, slots=True)
class ShareCounts:
    for_: Fraction
    against: Fraction
    abstain: Fraction
    base: Fraction


@dataclass(frozen=True, slots=True)
class Tally:
    persons: PersonCounts
    shares: ShareCounts
    person_ok: bool | None  # ölçüt aranmıyorsa None
    share_ok: bool | None
    accepted: bool


def owner_conflicts(roster: Sequence[RosterUnit], ballots: Mapping[uuid.UUID, Ballot]) -> list[str]:
    """Bir kişinin (malik kümesinin) bölümlerine farklı oy verilmiş kişi anahtarları."""
    seen: dict[str, Ballot] = {}
    clash: list[str] = []
    for unit in roster:
        choice = ballots.get(unit.unit_id)
        if choice is None:
            continue
        previous = seen.setdefault(unit.key, choice)
        if previous is not choice and unit.key not in clash:
            clash.append(unit.key)
    return clash


def missing_ballots(present: set[uuid.UUID], ballots: Mapping[uuid.UUID, Ballot]) -> int:
    return len(present - set(ballots))


def tally(
    roster: Sequence[RosterUnit],
    present: set[uuid.UUID],
    ballots: Mapping[uuid.UUID, Ballot],
    dtype: DecisionType,
) -> Tally:
    """Karar sayımı. Yalnız katılan bölümlerin oyu sayılır; kişi oyu, kişinin katılan bölümlerinin
    oyudur (aynı kişi tek oy — `owner_conflicts` önceden engeller)."""
    attending = [u for u in roster if u.unit_id in present]
    by_person: dict[str, Ballot] = {}
    share = {b: Fraction(0) for b in Ballot}
    for unit in attending:
        choice = ballots.get(unit.unit_id)
        if choice is None:
            continue
        by_person.setdefault(unit.key, choice)
        share[choice] += unit.share
    count = {b: sum(1 for v in by_person.values() if v is b) for b in Ballot}
    if dtype.base is DecisionBase.ALL:
        person_base = len({u.key for u in roster})
        share_base = total_share(roster)
    else:
        person_base = len({u.key for u in attending})
        share_base = total_share(attending)
        if not dtype.abstain_in_base:
            person_base -= count[Ballot.ABSTAIN]
            share_base -= share[Ballot.ABSTAIN]
    person_ok = (
        passes(
            Fraction(count[Ballot.FOR]),
            Fraction(person_base),
            dtype.num,
            dtype.den,
            strict=dtype.strict,
        )
        if dtype.person
        else None
    )
    share_ok = (
        passes(share[Ballot.FOR], share_base, dtype.num, dtype.den, strict=dtype.strict)
        if dtype.share
        else None
    )
    accepted = (dtype.person or dtype.share) and person_ok is not False and share_ok is not False
    return Tally(
        PersonCounts(count[Ballot.FOR], count[Ballot.AGAINST], count[Ballot.ABSTAIN], person_base),
        ShareCounts(share[Ballot.FOR], share[Ballot.AGAINST], share[Ballot.ABSTAIN], share_base),
        person_ok,
        share_ok,
        bool(accepted),
    )


# --- Vekâlet ---------------------------------------------------------------------------


def proxy_name_key(name: str) -> str:
    return tr_lower(" ".join(name.split()))


def proxy_load(
    roster: Sequence[RosterUnit],
    proxied_units: Iterable[uuid.UUID],
) -> int:
    """Vekilin temsil ettiği kişi (malik) sayısı — bölüm değil kişi sayılır."""
    keys = {u.unit_id: u.key for u in roster}
    return len({keys[uid] for uid in proxied_units if uid in keys})


# --- Çağrı süresi ----------------------------------------------------------------------


def notice_last_day(meeting_on: date, notice_days: int) -> date:
    """Çağrının en geç gönderileceği gün (toplantıdan `notice_days` gün önce)."""
    return meeting_on - timedelta(days=notice_days)


# --- Biçim ------------------------------------------------------------------------------


def pct_tr(value: Fraction, base: Fraction) -> str:
    """ "%38,52" — en çok iki ondalık, Türkçe virgül, sondaki sıfırlar atılır."""
    ratio = (
        Decimal(0)
        if base <= 0
        else Decimal(value.numerator * base.denominator)
        / Decimal(value.denominator * base.numerator)
    )
    text = str((ratio * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "%" + text.replace(".", ",")


def mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def no_quorum_message(q: QuorumResult) -> str:
    return (
        f"Toplantı nisabı sağlanamadı (kişi {q.persons_present}/{q.persons_total} "
        f"{mark(q.person_ok)}, arsa payı {pct_tr(q.share_present, q.share_total)} "
        f"{mark(q.share_ok)}). İkinci toplantı kuralını uygulayın."
    )


# --- Ayar doğrulama ---------------------------------------------------------------------


def bounded_int(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _ratio(raw: Mapping[str, object], key: str, errors: dict[str, str]) -> tuple[int, int] | None:
    den = bounded_int(raw.get("den"), 1, RATIO_DEN_MAX)
    num = bounded_int(raw.get("num"), 1, RATIO_DEN_MAX)
    if den is None or num is None or num > den:
        errors[key] = f"Oran 1/1 ile 1/{RATIO_DEN_MAX} arasında bir kesir olmalı (pay ≤ payda)."
        return None
    return num, den


def _flags(raw: Mapping[str, object], key: str, errors: dict[str, str]) -> tuple[bool, bool, bool]:
    values = [raw.get(k) for k in ("strict", "person", "share")]
    if not all(isinstance(v, bool) for v in values):
        errors[key] = "Eşik türü ve ölçütleri (kişi, arsa payı) seçin."
        return False, False, False
    strict, person, share = (bool(v) for v in values)
    if not (person or share):
        errors[key] = "Kişi sayısı ya da arsa payı ölçütünden en az biri seçilmeli."
    return strict, person, share


def check_threshold(raw: object, key: str, errors: dict[str, str]) -> Threshold | None:
    if not isinstance(raw, Mapping):
        errors[key] = "Eşik bilgisi eksik."
        return None
    ratio = _ratio(raw, key, errors)
    strict, person, share = _flags(raw, key, errors)
    if ratio is None or key in errors:
        return None
    return Threshold(ratio[0], ratio[1], strict, person, share)


def check_decision_types(raw: object, errors: dict[str, str]) -> tuple[DecisionType, ...] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= DECISION_TYPES_MAX:
        errors["decision_types"] = f"1 ile {DECISION_TYPES_MAX} arasında karar türü tanımlayın."
        return None
    out: list[DecisionType] = []
    codes: set[str] = set()
    for index, item in enumerate(raw):
        key = f"decision_types.{index}"
        if not isinstance(item, Mapping):
            errors[key] = "Karar türü bilgisi eksik."
            continue
        code = item.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            errors[f"{key}.code"] = "Kod küçük harf, rakam ve alt çizgi (2–30 karakter)."
            continue
        if code in codes:
            errors[f"{key}.code"] = "Bu kod iki kez kullanılmış."
            continue
        codes.add(code)
        name = " ".join(str(item.get("name") or "").split())
        if not NAME_MIN <= len(name) <= NAME_MAX:
            errors[f"{key}.name"] = f"Karar türüne ad verin ({NAME_MIN}–{NAME_MAX} karakter)."
        base = item.get("base")
        if base not in {b.value for b in DecisionBase}:
            errors[f"{key}.base"] = "Taban: katılanlar ya da tüm malikler."
        abstain = item.get("abstain_in_base", True)
        if not isinstance(abstain, bool):
            errors[f"{key}.abstain_in_base"] = "Çekimser oyun sayılışını seçin."
        ratio = _ratio(item, key, errors)
        strict, person, share = _flags(item, key, errors)
        if ratio is None or any(k.startswith(key) for k in errors):
            continue
        out.append(
            DecisionType(
                code,
                name,
                DecisionBase(str(base)),
                ratio[0],
                ratio[1],
                strict,
                person,
                share,
                bool(abstain),
            )
        )
    return tuple(out) if not any(k.startswith("decision_types") for k in errors) else None


def decision_types_of(raw: object) -> tuple[DecisionType, ...]:
    """Kayıtlı (doğrulanmış) JSON → karar türleri."""
    errors: dict[str, str] = {}
    checked = check_decision_types(raw, errors)
    return checked if checked is not None else DEFAULT_DECISION_TYPES


def threshold_of(raw: object, default: Threshold | None) -> Threshold | None:
    if raw is None:
        return None
    errors: dict[str, str] = {}
    return check_threshold(raw, "q", errors) or default
