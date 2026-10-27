"""KVKK saklama politikası, silme raporu ve VERBİS envanteri — Yazılım Dokümanı RAP-04, §4.4,
§10.2; PM kararı K8 (gerçek PostgreSQL). Kurgu `finance_world`, bugün 20.06.2026.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Registration, RetentionRun, SecuritySettings
from site_yonetim.services import retention as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def _rejected(w: World, number: int, decided: datetime, site_id: object = None) -> None:
    with site_scope(site_id or w.site_id):  # type: ignore[arg-type]
        async with w.factory() as session, session.begin():
            session.add(
                Registration(
                    number=number, first_name="Deniz", last_name="Ak",
                    phone=f"+90532000000{number}",
                    email="deniz@test.local", unit_text="A 9", relation="tenant", status="rejected",
                    kvkk_ack_at=decided, decided_at=decided, decided_by_name="Kerem",
                    reject_reason="Bölüm bulunamadı", ip="203.0.113.9",
                )
            )  # fmt: skip


def _by_key(body: dict[str, object]) -> dict[str, dict[str, object]]:
    return {c["key"]: c for c in body["categories"]}  # type: ignore[attr-defined]


async def test_politika_varsayilanlari_dokumandan(world: World) -> None:
    body = (await world.get("/settings/retention")).json()
    rows = _by_key(body)
    assert list(rows) == ["visitors", "registrations", "finance", "employment"]
    visitors = rows["visitors"]
    assert (visitors["value"], visitors["unit"], visitors["max_value"]) == (6, "month", 24)
    assert (visitors["auto_apply"], visitors["needs_legal_review"]) == (False, True)
    assert visitors["cutoff"] == "2025-12-20"
    finance = rows["finance"]
    assert (finance["value"], finance["unit"], finance["adjustable"]) == (10, "year", False)
    assert finance["action"] == "keep"
    assert finance["pending"] is None


async def test_ziyaretci_suresi_guvenlik_ayariyla_ayni(world: World) -> None:
    """İkileme yok: KVKK ekranındaki ziyaretçi süresi güvenlik ayarının kendisidir."""
    saved = await world.api.put(
        world.url("/settings/retention"),
        json={"items": [{"key": "visitors", "value": 12, "auto_apply": True},
                        {"key": "registrations", "value": 3}]},
        headers=world.headers,
    )  # fmt: skip
    assert saved.status_code == 200, saved.text
    rows = _by_key(saved.json()["data"])
    assert (rows["visitors"]["value"], rows["visitors"]["auto_apply"]) == (12, True)
    assert (rows["registrations"]["value"], rows["registrations"]["auto_apply"]) == (3, False)
    with site_scope(world.site_id):
        async with world.factory() as session:
            security = await session.scalar(select(SecuritySettings))
    assert security is not None
    assert (security.retention_months, security.auto_anonymize, security.version) == (12, True, 1)


async def test_politika_dogrulama(world: World) -> None:
    response = await world.api.put(
        world.url("/settings/retention"),
        json={"items": [{"key": "visitors", "value": 25}, {"key": "finance", "value": 5},
                        {"key": "registrations", "value": 0}]},
        headers=world.headers,
    )  # fmt: skip
    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert fields["visitors"] == "Süre en fazla 24 ay olabilir."
    assert fields["registrations"] == "Süre en az 1 ay olmalı."
    assert "ayarlanamaz" in fields["finance"]


async def test_elle_uygulama_ve_silme_raporu(world: World) -> None:
    await _rejected(world, 1, datetime(2025, 11, 1, 9, tzinfo=UTC))  # 6 aydan eski
    await _rejected(world, 2, datetime(2026, 6, 1, 9, tzinfo=UTC))  # yeni
    await _rejected(world, 3, datetime(2025, 1, 1, 9, tzinfo=UTC), world.other_site_id)
    rows = _by_key((await world.get("/settings/retention")).json())
    assert rows["registrations"]["pending"] == 1

    applied = await world.post("/settings/retention/registrations/apply")
    assert applied.status_code == 200, applied.text
    run = applied.json()["data"]
    assert (run["affected"], run["trigger"], run["action"]) == (1, "manual", "anonymize")
    with site_scope(world.site_id):
        async with world.factory() as session:
            regs = {r.number: r for r in await session.scalars(select(Registration))}
    assert (regs[1].first_name, regs[1].phone, regs[1].email, regs[1].ip) == (
        "Anonim", "", None, None
    )  # fmt: skip
    assert regs[2].first_name == "Deniz"
    # Başka site etkilenmez
    with site_scope(world.other_site_id):
        async with world.factory() as session:
            other = await session.scalar(select(Registration))
    assert other is not None
    assert other.first_name == "Deniz"

    report = (await world.get("/settings/retention/runs")).json()
    assert report["total"] == 1
    assert report["items"][0]["category_label"] == "Reddedilen kayıt başvuruları"
    assert (await world.post("/settings/retention/finance/apply")).status_code == 404
    other_runs = await world.api.get(
        world.url("/settings/retention/runs", "yildiz-sitesi"), headers=world.headers
    )
    assert other_runs.json()["total"] == 0


async def test_gece_isi_yalniz_otomatik_acik_kategoride(world: World) -> None:
    await _rejected(world, 1, datetime(2025, 11, 1, 9, tzinfo=UTC))
    now = datetime(2026, 6, 20, 0, 30, tzinfo=UTC)
    assert await svc.run_nightly(world.factory, today=date(2026, 6, 20), now=now) == []
    await world.api.put(
        world.url("/settings/retention"),
        json={"items": [{"key": "registrations", "value": 6, "auto_apply": True}]},
        headers=world.headers,
    )
    results = await svc.run_nightly(world.factory, today=date(2026, 6, 20), now=now)
    assert [(r.site_id, r.category, r.affected) for r in results] == [
        (world.site_id, "registrations", 1)
    ]
    with site_scope(world.site_id):
        async with world.factory() as session:
            runs = list(await session.scalars(select(RetentionRun)))
    assert [(r.trigger, r.actor_name) for r in runs] == [("job", "KVKK saklama işi")]


async def test_veri_envanteri_ve_disa_aktarma(world: World) -> None:
    rows = (await world.get("/data-inventory")).json()
    labels = [r["category_label"] for r in rows]
    assert {"Kimlik", "İletişim", "Finans", "Konum / plaka", "Özlük"} <= set(labels)
    finance = next(r for r in rows if r["category"] == "finance")
    assert finance["records"] == 2  # A-1 maliki: M + O hesapları
    visitors = next(r for r in rows if r["source"].startswith("Ziyaretçi"))
    assert visitors["retention"] == "6 ay; anonimleştirilir"

    csv = await world.get("/data-inventory/export", params={"format": "csv"})
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.content.startswith("﻿".encode())
    assert "KVKK kategorisi;Veri" in csv.content.decode("utf-8")
    xlsx = await world.get("/data-inventory/export")
    assert xlsx.content[:2] == b"PK"


async def test_saklama_yetkisi(world: World) -> None:
    user = await create_user(world.factory, "muhasebe@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Muhasebe")
    headers = await login_headers(world.api, "muhasebe@test.local")
    for path in ("/settings/retention", "/data-inventory", "/settings/retention/runs"):
        assert (await world.api.get(world.url(path), headers=headers)).status_code == 403
