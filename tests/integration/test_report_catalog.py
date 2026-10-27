"""Rapor kataloğu, talep/SLA raporu, katalog dışa aktarma ve denetim paketi — Yazılım Dokümanı
RAP-01, RAP-05; servis istekleri 79, 81 (gerçek PostgreSQL). Kurgu `finance_world`.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import (
    Document,
    Meeting,
    MeetingAgendaItem,
    Plan,
    Request,
    RequestCategory,
    RequestFeedback,
    Site,
)
from site_yonetim.services.sites import set_module_enabled
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

T0 = datetime(2026, 6, 1, 9, tzinfo=UTC)


async def _role(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def _modules(w: World, *keys: ModuleKey) -> None:
    async with w.factory() as session, session.begin():
        plan = Plan(name=f"P {uuid.uuid4().hex[:6]}", max_units=150,
                    allowed_modules=["finance", *(k.value for k in keys)])  # fmt: skip
        session.add(plan)
        await session.flush()
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.plan_id = plan.id
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            site = await session.get(Site, w.site_id)
            assert site is not None
            for key in keys:
                await set_module_enabled(session, site, key, enable=True)


# --- Katalog --------------------------------------------------------------------------------


async def test_katalog_yetki_ve_modulle_suzulur(world: World) -> None:
    keys = [r["key"] for r in (await world.get("/reports/catalog")).json()]
    # Talep modülü kapalı: talep/SLA katalogda yok
    assert "requests_sla" not in keys
    assert {"income_expense", "collections", "cash", "audit"} <= set(keys)
    await _modules(world, ModuleKey.REQUESTS, ModuleKey.VISITORS)
    manager = (await world.get("/reports/catalog")).json()
    sla = next(r for r in manager if r["key"] == "requests_sla")
    assert sla["exportable"] is True
    assert sla["metrics"][0]["name"] == "SLA uyumu"
    assert "SLA süresi içinde" in sla["metrics"][0]["definition"]

    auditor = await _role(world, "denetci@test.local", "Denetçi")
    seen = {r["group"] for r in (await world.api.get(world.url("/reports/catalog"),
                                                     headers=auditor)).json()}  # fmt: skip
    assert seen == {"Finans", "Satın alma", "Yönetim"}  # RAP-01: denetçi
    board = await _role(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    board_keys = [r["key"] for r in (await world.api.get(world.url("/reports/catalog"),
                                                         headers=board)).json()]  # fmt: skip
    assert "security" not in board_keys  # RAP-01 S1
    assert "requests_sla" in board_keys


# --- Talep ve SLA ---------------------------------------------------------------------------


async def _requests(w: World) -> None:
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            keys = list(await session.scalars(select(RequestCategory.key)))
            assert {"plumbing", "elevator"} <= set(keys)
            rows = [
                # (kategori, açılış, çözüm, durum, son tarih, puan)
                ("plumbing", T0, T0 + timedelta(hours=4), "closed", T0 + timedelta(hours=8), 5),
                ("plumbing", T0, T0 + timedelta(hours=10), "resolved", T0 + timedelta(hours=8), 3),
                ("plumbing", T0, None, "in_progress", T0 + timedelta(hours=8), None),
                # bozuk kayıt: çözüm açılıştan önce — ortalamaya girmez
                ("elevator", T0, T0 - timedelta(hours=2), "closed", None, None),
                ("elevator", T0 - timedelta(days=90), None, "open", None, None),  # dönem dışı
            ]
            for n, (category, created, resolved, status, due, score) in enumerate(rows, start=1):
                request = Request(
                    number=n, title=f"Talep {n}", category=category, status=status,
                    resolution="Giderildi" if status in ("resolved", "closed") else None,
                    resolved_at=resolved, due_at=due,
                    sla_hour_type="24x7" if due else None, sla_target_hours=8 if due else None,
                )  # fmt: skip
                session.add(request)
                await session.flush()
                request.created_at = created
                if score is not None:
                    session.add(
                        RequestFeedback(
                            request_id=request.id, solved=True, score=score, by_name="Ayşe"
                        )
                    )


async def test_talep_sla_raporu(world: World) -> None:
    assert (await world.get("/reports/requests-sla")).status_code == 404  # modül kapalı
    await _modules(world, ModuleKey.REQUESTS)
    await _requests(world)
    body = (await world.get("/reports/requests-sla", params={"to": "2026-06-20"})).json()
    assert (body["from"], body["to"]) == ("2026-05-22", "2026-06-20")
    assert body["total"] == {
        "total": 4, "open": 1, "closed": 3, "sla_rate": "50.0", "avg_hours": "7.0",
        "satisfaction": "4.0", "rated": 2,
    }  # fmt: skip
    rows = {r["category"]: r for r in body["rows"]}
    assert rows["plumbing"]["category_name"] == "Su / tesisat"
    assert (rows["elevator"]["closed"], rows["elevator"]["avg_hours"]) == (1, None)

    wrong = await world.get(
        "/reports/requests-sla", params={"from": "2026-06-21", "to": "2026-06-20"}
    )
    assert wrong.status_code == 422
    auditor = await _role(world, "denetci@test.local", "Denetçi")
    denied = await world.api.get(world.url("/reports/requests-sla"), headers=auditor)
    assert denied.status_code == 403
    # Başka site: kendi verisi (talep yok)
    other = await world.api.get(world.url("/reports/requests-sla", "yildiz-sitesi"),
                                headers=world.headers)  # fmt: skip
    assert other.status_code == 404  # Yıldız'da modül kapalı


async def test_katalog_raporu_excel(world: World) -> None:
    await _modules(world, ModuleKey.REQUESTS)
    await _requests(world)
    xlsx = await world.get(
        "/reports/requests_sla/export.xlsx", params={"from": "2026-06-01", "to": "2026-06-20"}
    )
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"
    finance = await world.get("/reports/income_expense/export.xlsx")
    assert finance.status_code == 200
    assert (await world.get("/reports/yok/export.xlsx")).status_code == 404
    guard = await _role(world, "guvenlik@test.local", "Güvenlik")
    denied = await world.api.get(world.url("/reports/cash/export.xlsx"), headers=guard)
    assert denied.status_code == 403
    # mevcut gelir–gider Excel ucu değişmedi
    assert (await world.get("/reports/income-expense/export.xlsx")).status_code == 200


# --- Denetim kurulu paketi ------------------------------------------------------------------


async def _decision_and_documents(w: World) -> tuple[str, str]:
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            meeting = Meeting(number=1, kind="board", title="Yönetim kurulu", status="held",
                              scheduled_at=T0, held_at=T0, location="Salon")  # fmt: skip
            session.add(meeting)
            await session.flush()
            session.add(
                MeetingAgendaItem(
                    meeting_id=meeting.id, order=1, title="Boya işi", result="accepted",
                    decision="Teklif kabul edildi.",
                )
            )  # fmt: skip
            docs = [
                Document(title=t, title_key=t.lower(), kind=k, scope="site", confidentiality=c,
                         current_version=1)
                for t, k, c in (("Yönetim planı", "plan", "public"),
                                ("Denetim raporu 2026", "financial", "management"))
            ]  # fmt: skip
            session.add_all(docs)
            await session.flush()
            return str(docs[0].id), str(docs[1].id)


async def test_denetim_paketi(world: World) -> None:
    plan_doc, report_doc = await _decision_and_documents(world)
    bad = await world.post(
        "/audit-packages", {"title": "x", "from": "2026-06-30", "to": "2026-01-01"}
    )
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"title", "to"}
    missing = await world.post(
        "/audit-packages",
        {"title": "2026 denetimi", "from": "2026-01-01", "to": "2026-06-30",
         "document_ids": [str(uuid.uuid4())]},
    )  # fmt: skip
    assert missing.status_code == 422

    created = await world.post(
        "/audit-packages",
        {"title": "2026 ilk yarı", "from": "2026-01-01", "to": "2026-06-30",
         "document_ids": [plan_doc]},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    package = created.json()["data"]
    assert (package["from"], package["status"]) == ("2026-01-01", "open")
    assert [d["item_title"] for d in package["decisions"]] == ["Boya işi"]
    assert package["documents"] == [{"id": plan_doc, "title": "Yönetim planı"}]
    assert package["snapshot"]["income"] == "0.00"
    assert [m["period"] for m in package["snapshot"]["months"]][:2] == ["01/2026", "02/2026"]

    accounting = await _role(world, "muhasebe@test.local", "Muhasebe")
    url = world.url(f"/audit-packages/{package['id']}/report")
    denied = await world.api.post(url, json={"document_id": report_doc}, headers=accounting)
    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "Denetim raporunu denetçi yükler."
    auditor = await _role(world, "denetci@test.local", "Denetçi")
    listed = await world.api.get(world.url("/audit-packages"), headers=auditor)
    assert listed.json()["total"] == 1
    no_doc = await world.api.post(url, json={"note": "x"}, headers=auditor)
    assert no_doc.status_code == 422
    reviewed = await world.api.post(
        url, json={"document_id": report_doc, "note": "Uygun"}, headers=auditor
    )
    assert reviewed.status_code == 200, reviewed.text
    data = reviewed.json()["data"]
    assert (data["status"], data["report_document_title"], data["note"]) == (
        "reviewed", "Denetim raporu 2026", "Uygun"
    )  # fmt: skip
    again = await world.api.post(url, json={"document_id": report_doc}, headers=auditor)
    assert again.status_code == 409

    xlsx = await world.get(f"/audit-packages/{package['id']}/export.xlsx")
    assert xlsx.content[:2] == b"PK"
    guard = await _role(world, "guvenlik@test.local", "Güvenlik")
    assert (await world.api.get(world.url("/audit-packages"), headers=guard)).status_code == 403
    # Başka site
    other = await world.api.get(
        world.url("/audit-packages", "yildiz-sitesi"), headers=world.headers
    )
    assert other.json()["total"] == 0
    cross = await world.api.get(
        world.url(f"/audit-packages/{package['id']}/export.xlsx", "yildiz-sitesi"),
        headers=world.headers,
    )
    assert cross.status_code == 404


async def test_gizli_belge_yalniz_yetkiliye(world: World) -> None:
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            secret = Document(title="Gizli", title_key="gizli", kind="other", scope="site",
                              confidentiality="secret", current_version=1)  # fmt: skip
            session.add(secret)
            await session.flush()
            secret_id = str(secret.id)
    accounting = await _role(world, "muhasebe@test.local", "Muhasebe")
    response = await world.api.post(
        world.url("/audit-packages"),
        json={"title": "Paket", "from": "2026-01-01", "to": "2026-03-31",
              "document_ids": [secret_id]},
        headers=accounting,
    )  # fmt: skip
    assert response.status_code == 422
    ok = await world.post(
        "/audit-packages",
        {"title": "Paket", "from": "2026-01-01", "to": "2026-03-31", "document_ids": [secret_id]},
    )
    assert ok.status_code == 201
