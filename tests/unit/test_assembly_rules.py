"""Genel kurul nisap ve çoğunluk hesabı — Yazılım Dokümanı TOP-03 (S1) ve servis isteği 75–76
örnekleri birebir; kişi + arsa payı, çekimser oy, ikinci toplantı, kişi sayımı, vekâlet yükü."""

import uuid
from fractions import Fraction

import pytest

from site_yonetim.domain import assembly as rules
from site_yonetim.domain.assembly import (
    DEFAULT_DECISION_TYPES,
    DEFAULT_QUORUM,
    Ballot,
    DecisionBase,
    DecisionType,
    PersonRule,
    RosterUnit,
    Threshold,
)

TYPES = {t.code: t for t in DEFAULT_DECISION_TYPES}


def roster(shares: list[Fraction]) -> list[RosterUnit]:
    """Her bölüm ayrı malik (kişi)."""
    out = []
    for share in shares:
        uid = uuid.uuid4()
        out.append(
            RosterUnit(uid, rules.person_key(uid, [uuid.uuid4()], PersonRule.OWNER_GROUP), share)
        )
    return out


def present(units: list[RosterUnit], count: int) -> set[uuid.UUID]:
    return {u.unit_id for u in units[:count]}


def test_s1_kisi_nisabi_saglanir_pay_nisabi_saglanmaz() -> None:
    """S1: 100 malik (arsa payı toplamı 1000); katılan 52 kişi ve 480 pay → nisap sağlanamadı."""
    shares = [Fraction(480, 52 * 1000)] * 52 + [Fraction(520, 48 * 1000)] * 48
    units = roster(shares)
    q = rules.quorum(units, present(units, 52), DEFAULT_QUORUM)
    assert (q.persons_present, q.persons_total) == (52, 100)
    assert (q.share_present, q.share_total) == (Fraction(480, 1000), Fraction(1))
    assert q.person_ok is True  # 52 > 50
    assert q.share_ok is False  # 480 < 500
    assert (q.required, q.ok) == (True, False)


def test_servis_istegi_46_malik_24_kisi_yuzde_38_52_pay() -> None:
    """46 malikten 24'ü (kişi ✓) ama arsa payının %38,52'si (pay ✗) → mesaj birebir."""
    shares = [Fraction(160, 10000)] * 23 + [Fraction(172, 10000)]
    shares += [Fraction(280, 10000)] * 21 + [Fraction(268, 10000)]
    units = roster(shares)
    assert rules.total_share(units) == 1
    q = rules.quorum(units, present(units, 24), DEFAULT_QUORUM)
    assert (q.person_ok, q.share_ok, q.ok) == (True, False, False)
    assert rules.no_quorum_message(q) == (
        "Toplantı nisabı sağlanamadı (kişi 24/46 ✓, arsa payı %38,52 ✗). "
        "İkinci toplantı kuralını uygulayın."
    )


def test_tam_yarisi_salt_cogunluk_degildir() -> None:
    units = roster([Fraction(1, 10)] * 10)
    q = rules.quorum(units, present(units, 5), DEFAULT_QUORUM)
    assert (q.person_ok, q.share_ok, q.ok) == (False, False, False)
    q = rules.quorum(units, present(units, 6), DEFAULT_QUORUM)
    assert q.ok is True


def test_ikinci_toplantida_nisap_aranmaz_ya_da_parametreyle_aranir() -> None:
    units = roster([Fraction(1, 10)] * 10)
    q = rules.quorum(units, present(units, 2), DEFAULT_QUORUM, second_call=True)
    assert (q.required, q.ok) == (False, True)
    only_people = Threshold(1, 2, True, True, False)
    q = rules.quorum(
        units, present(units, 2), DEFAULT_QUORUM, second_call=True, second_call_rule=only_people
    )
    assert (q.required, q.ok) == (True, False)
    q = rules.quorum(
        units, present(units, 6), DEFAULT_QUORUM, second_call=True, second_call_rule=only_people
    )
    assert q.ok is True


def test_dort_bese_33_46_kisi_yuzde_69_pay_ret() -> None:
    """Servis isteği 76: 4/5 maddesi 33/46 kişi ve %69 pay kabulle RET."""
    shares = [Fraction(69, 100 * 33)] * 33 + [Fraction(31, 100 * 13)] * 13
    units = roster(shares)
    everyone = present(units, 46)
    ballots = {u.unit_id: Ballot.FOR for u in units[:33]} | {
        u.unit_id: Ballot.AGAINST for u in units[33:]
    }
    t = rules.tally(units, everyone, ballots, TYPES["four_fifths"])
    assert (t.persons.for_, t.persons.base) == (33, 46)
    assert t.shares.for_ == Fraction(69, 100)
    assert (t.person_ok, t.share_ok, t.accepted) == (False, False, False)


def test_dort_bese_sinir_en_az_esittir() -> None:
    units = roster([Fraction(1, 5)] * 5)
    ballots = {u.unit_id: Ballot.FOR for u in units[:4]}
    t = rules.tally(units, present(units, 4), ballots, TYPES["four_fifths"])
    assert (t.person_ok, t.share_ok, t.accepted) == (True, True, True)  # 4/5 ≥ 4/5
    t = rules.tally(units, present(units, 4), dict(list(ballots.items())[:3]), TYPES["four_fifths"])
    assert t.accepted is False


def test_oy_coklugu_cekimser_tabanda_kalir_parametreyle_cikar() -> None:
    units = roster([Fraction(1, 30)] * 30)
    ballots = (
        {u.unit_id: Ballot.FOR for u in units[:15]}
        | {u.unit_id: Ballot.AGAINST for u in units[15:26]}
        | {u.unit_id: Ballot.ABSTAIN for u in units[26:]}
    )
    ordinary = TYPES["ordinary"]
    t = rules.tally(units, present(units, 30), ballots, ordinary)
    assert (t.persons.for_, t.persons.against, t.persons.abstain, t.persons.base) == (15, 11, 4, 30)
    assert t.accepted is False  # 15/30 yarıdan fazla değil — çekimser fiilen ret etkisi
    excluded = DecisionType(
        **{**ordinary.as_dict(), "base": DecisionBase.PRESENT, "abstain_in_base": False}
    )
    t = rules.tally(units, present(units, 30), ballots, excluded)
    assert (t.persons.base, t.shares.base) == (26, Fraction(26, 30))
    assert t.accepted is True


def test_tum_maliklerin_cogunlugu_katilmayani_tabanda_sayar() -> None:
    units = roster([Fraction(1, 10)] * 10)
    ballots = {u.unit_id: Ballot.FOR for u in units[:5]}
    t = rules.tally(units, present(units, 6), ballots, TYPES["all_majority"])
    assert t.persons.base == 10
    assert t.accepted is False  # 5/10 yarıdan fazla değil
    t = rules.tally(units, present(units, 6), ballots, TYPES["ordinary"])
    assert t.accepted is True  # katılan 6'nın 5'i


def test_oy_birligi_yalniz_kisi_olcutu() -> None:
    units = roster([Fraction(1, 4)] * 4)
    everyone = present(units, 4)
    t = rules.tally(units, everyone, dict.fromkeys(everyone, Ballot.FOR), TYPES["unanimous"])
    assert (t.person_ok, t.share_ok, t.accepted) == (True, None, True)
    t = rules.tally(
        units, present(units, 3), dict.fromkeys(present(units, 3), Ballot.FOR), TYPES["unanimous"]
    )
    assert t.accepted is False


def test_kisi_sayimi_hisseli_ve_cok_bolumlu_malik() -> None:
    ali, ayse, can = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    u1, u2, u3, u4 = (uuid.uuid4() for _ in range(4))
    rule = PersonRule.OWNER_GROUP
    units = [
        RosterUnit(u1, rules.person_key(u1, [ali], rule), Fraction(1, 4)),
        RosterUnit(u2, rules.person_key(u2, [ali], rule), Fraction(1, 4)),  # Ali'nin 2. bölümü
        RosterUnit(u3, rules.person_key(u3, [ayse, can], rule), Fraction(1, 4)),  # hisseli
        RosterUnit(u4, rules.person_key(u4, [can, ayse], rule), Fraction(1, 4)),  # aynı küme
    ]
    q = rules.quorum(units, {u1, u2}, DEFAULT_QUORUM)
    assert (q.persons_present, q.persons_total) == (1, 2)
    assert (q.person_ok, q.share_ok) == (False, False)  # kişi 1/2, pay 1/2 — yarıdan fazla değil
    per_unit = [
        RosterUnit(u.unit_id, rules.person_key(u.unit_id, [ali], PersonRule.PER_UNIT), u.share)
        for u in units
    ]
    assert rules.quorum(per_unit, {u1, u2, u3}, DEFAULT_QUORUM).persons_total == 4
    # aynı kişinin iki bölümü tek oy: kişi sayımında bir kez
    t = rules.tally(
        units,
        {u1, u2, u3, u4},
        {u1: Ballot.FOR, u2: Ballot.FOR, u3: Ballot.AGAINST, u4: Ballot.AGAINST},
        TYPES["ordinary"],
    )
    assert (t.persons.for_, t.persons.against, t.persons.base) == (1, 1, 2)
    assert t.shares.for_ == Fraction(1, 2)
    assert rules.owner_conflicts(units, {u1: Ballot.FOR, u2: Ballot.AGAINST}) == [units[0].key]
    assert rules.owner_conflicts(units, {u1: Ballot.FOR, u3: Ballot.AGAINST}) == []


def test_vekalet_yuku_kisi_sayar() -> None:
    ali = uuid.uuid4()
    u1, u2, u3 = (uuid.uuid4() for _ in range(3))
    rule = PersonRule.OWNER_GROUP
    units = [
        RosterUnit(u1, rules.person_key(u1, [ali], rule), Fraction(1, 3)),
        RosterUnit(u2, rules.person_key(u2, [ali], rule), Fraction(1, 3)),
        RosterUnit(u3, rules.person_key(u3, [uuid.uuid4()], rule), Fraction(1, 3)),
    ]
    assert rules.proxy_load(units, [u1, u2]) == 1
    assert rules.proxy_load(units, [u1, u2, u3]) == 2
    assert rules.proxy_name_key("  Mehmet   İLHAN ") == rules.proxy_name_key("mehmet ilhan")


def test_oran_ve_bicim() -> None:
    assert rules.passes(Fraction(0), Fraction(0), 1, 2, strict=False) is False
    assert rules.pct_tr(Fraction(1, 2), Fraction(1)) == "%50"
    assert rules.pct_tr(Fraction(1, 3), Fraction(1)) == "%33,33"
    assert rules.pct_tr(Fraction(3852, 10000), Fraction(1)) == "%38,52"
    assert rules.pct_tr(Fraction(1), Fraction(0)) == "%0"


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ([], "decision_types"),
        (
            [
                {
                    "code": "X",
                    "name": "Ad",
                    "base": "present",
                    "num": 1,
                    "den": 2,
                    "strict": True,
                    "person": True,
                    "share": True,
                }
            ],
            "decision_types.0.code",
        ),
        (
            [
                {
                    "code": "aa",
                    "name": "Ad",
                    "base": "x",
                    "num": 1,
                    "den": 2,
                    "strict": True,
                    "person": True,
                    "share": True,
                }
            ],
            "decision_types.0.name",
        ),
        (
            [
                {
                    "code": "aa",
                    "name": "Adı",
                    "base": "all",
                    "num": 3,
                    "den": 2,
                    "strict": True,
                    "person": True,
                    "share": True,
                }
            ],
            "decision_types.0",
        ),
        (
            [
                {
                    "code": "aa",
                    "name": "Adı",
                    "base": "all",
                    "num": 1,
                    "den": 2,
                    "strict": True,
                    "person": False,
                    "share": False,
                }
            ],
            "decision_types.0",
        ),
    ],
)
def test_karar_turu_dogrulama(raw: object, key: str) -> None:
    errors: dict[str, str] = {}
    assert rules.check_decision_types(raw, errors) is None
    assert key in errors


def test_varsayilan_karar_turleri_gidip_gelir() -> None:
    raw = [t.as_dict() for t in DEFAULT_DECISION_TYPES]
    assert rules.decision_types_of(raw) == DEFAULT_DECISION_TYPES
    assert rules.decision_types_of("bozuk") == DEFAULT_DECISION_TYPES
    assert rules.threshold_of(DEFAULT_QUORUM.as_dict(), None) == DEFAULT_QUORUM
    assert rules.threshold_of(None, DEFAULT_QUORUM) is None
