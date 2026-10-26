"""Planlı raporlar — Yazılım Dokümanı RAP-05 (e-posta test sağlayıcısıyla, K5).

Gerçek PostgreSQL.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import NotificationDelivery, ScheduledReportRun, SiteMembership
from site_yonetim.services import scheduled_reports as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


def body(recipients: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "name": "Aylık tahsilat",
        "report_key": "collections",
        "frequency": "monthly",
        "day_of_month": 1,
        "send_time": "08:30",
        "format": "pdf",
        "recipient_user_ids": recipients,
        **overrides,
    }


async def options(w: World) -> dict[str, Any]:
    response = await w.get("/scheduled-reports/options")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


async def test_tanim_yetkili_alici_ve_gonderim(world: World) -> None:
    tech = await create_user(world.factory, "teknik@test.local", full_name="Teknik KİŞİ")
    await add_site_membership(world.factory, world.site_id, tech.id, "Teknik Personel")
    opts = await options(world)
    assert [k["key"] for k in opts["kinds"]][:2] == ["income_expense", "collections"]
    finance_people = [r["full_name"] for r in opts["recipients"]["collections"]]
    assert finance_people == ["Kerem YILDIRIM"]  # teknik personel finans raporu alamaz
    kerem = opts["recipients"]["collections"][0]["user_id"]

    unauthorized = await world.post("/scheduled-reports", body([kerem, str(tech.id)]))
    assert unauthorized.status_code == 422
    assert "recipient_user_ids" in unauthorized.json()["error"]["fields"]
    weekly_without_day = await world.post(
        "/scheduled-reports", body([kerem], frequency="weekly", day_of_month=None)
    )
    assert weekly_without_day.json()["error"]["fields"] == {
        "weekday": "Haftalık raporda gönderim gününü seçin."
    }

    created = await world.post("/scheduled-reports", body([kerem]))
    assert created.status_code == 201, created.text
    report = created.json()["data"]
    assert report["frequency_label"] == "Aylık"
    assert report["send_time"] == "08:30"
    assert report["recipients"][0]["has_email"] is True
    listed = (await world.get("/scheduled-reports")).json()
    assert listed["total"] == 1

    # zamanı gelince iş çalışır; aynı zaman dilimi ikinci kez çalışmaz
    due = datetime.fromisoformat(report["next_run_at"])
    first = await svc.run_due(world.factory, now=due, web_url="http://localhost:5173")
    assert first == [(world.site_id, 1)]
    again = await svc.run_due(world.factory, now=due, web_url="http://localhost:5173")
    assert again == []
    with site_scope(world.site_id):
        async with world.factory() as session:
            [run] = list(await session.scalars(select(ScheduledReportRun)))
            [mail] = list(await session.scalars(select(NotificationDelivery)))
    assert (run.status, run.recipient_count) == ("sent", 1)
    assert (mail.channel, mail.recipient, mail.kind) == (
        "email", "yonetici@test.local", "report.collections"
    )  # fmt: skip
    runs = (await world.get(f"/scheduled-reports/{report['id']}/runs")).json()
    assert runs["items"][0]["status"] == "sent"

    now_run = await world.post(f"/scheduled-reports/{report['id']}/run")
    assert now_run.status_code == 200, now_run.text
    assert now_run.json()["data"]["recipient_count"] == 1

    updated = await world.api.put(
        world.url(f"/scheduled-reports/{report['id']}"),
        json=body([kerem], frequency="daily", day_of_month=None, is_active=False),
        headers=world.headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["is_active"] is False
    assert (await world.api.delete(
        world.url(f"/scheduled-reports/{report['id']}"), headers=world.headers
    )).status_code == 204  # fmt: skip


async def test_yetki_ve_izolasyon(world: World) -> None:
    tech = await create_user(world.factory, "teknik@test.local", full_name="Teknik KİŞİ")
    await add_site_membership(world.factory, world.site_id, tech.id, "Teknik Personel")
    headers = await login_headers(world.api, "teknik@test.local")
    assert (
        await world.api.get(world.url("/scheduled-reports"), headers=headers)
    ).status_code == 403
    kerem = (await options(world))["recipients"]["collections"][0]["user_id"]
    created = (await world.post("/scheduled-reports", body([kerem]))).json()["data"]
    other = await world.api.get(
        world.url("/scheduled-reports", "yildiz-sitesi"), headers=world.headers
    )
    assert other.json()["total"] == 0
    missing = await world.api.get(
        world.url(f"/scheduled-reports/{created['id']}/runs", "yildiz-sitesi"),
        headers=world.headers,
    )
    assert missing.status_code == 404


async def test_alicinin_yetkisi_kalkarsa_gonderilmez(world: World) -> None:
    acc = await create_user(world.factory, "muhasebe@test.local", full_name="Selin ARI")
    await add_site_membership(world.factory, world.site_id, acc.id, "Muhasebe")
    created = (await world.post("/scheduled-reports", body([str(acc.id)]))).json()["data"]
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            await session.execute(
                update(SiteMembership)
                .where(SiteMembership.user_id == acc.id)
                .values(role="Teknik Personel")
            )
    run = await world.post(f"/scheduled-reports/{created['id']}/run")
    assert run.json()["data"]["status"] == "skipped"
    assert run.json()["message"] == "Gönderilmedi: yetkili ve e-postası olan alıcı yok."
