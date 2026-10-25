"""Belge arşivi — Yazılım Dokümanı RAP-03, 18.2; frontend servis isteği 78 (gerçek PostgreSQL).

Kurgu `finance_world`: Aksu'da A blok, A-1 maliki Ayşe; bugün 20.06.2026.
"""

import asyncio
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim import cli
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import AuditLog, Notification, Plan, Site
from site_yonetim.services import documents as svc
from site_yonetim.services.files import FileStore
from site_yonetim.services.sites import set_module_enabled
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF_2 = b"%PDF-1.4\n% ikinci surum\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def _documents_on(w: World, site_id: uuid.UUID | None = None) -> None:
    async with w.factory() as session, session.begin():
        plan = Plan(name=f"Belge {uuid.uuid4().hex[:6]}", max_units=150,
                    allowed_modules=["finance", "documents"])  # fmt: skip
        session.add(plan)
        await session.flush()
        for sid in (w.site_id, w.other_site_id):
            site = await session.get(Site, sid)
            assert site is not None
            site.plan_id = plan.id
    for sid in (site_id or w.site_id, w.other_site_id):
        with site_scope(sid):
            async with w.factory() as session, session.begin():
                site = await session.get(Site, sid)
                assert site is not None
                await set_module_enabled(session, site, ModuleKey.DOCUMENTS, enable=True)


@pytest.fixture
async def arsiv(world: World, api_app: FastAPI, tmp_path: Path) -> World:
    api_app.dependency_overrides[get_file_store] = lambda: FileStore(tmp_path)
    await _documents_on(world)
    return world


async def upload(
    w: World,
    *,
    headers: dict[str, str] | None = None,
    file: tuple[str, bytes] = ("plan.pdf", PDF),
    slug: str = "aksu-konaklari",
    **fields: Any,
) -> httpx2.Response:
    data = {
        "title": "Yönetim planı",
        "kind": "plan",
        "scope": "site",
        "confidentiality": "public",
        **{k: v for k, v in fields.items() if v is not None},
    }
    return await w.api.post(
        w.url("/documents", slug),
        data=data,
        files={"file": (file[0], file[1], "application/octet-stream")},
        headers=headers or w.headers,
    )


async def _staff(w: World, email: str, role: str) -> dict[str, str]:
    return (await _staff_user(w, email, role))[1]


async def _staff_user(w: World, email: str, role: str) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, email, full_name=f"{role} KİŞİ")
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return user.id, await login_headers(w.api, email)


async def _resident(w: World) -> dict[str, str]:
    user = await create_user(w.factory, "ayse@test.local", kind="resident")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    return await login_headers(w.api, "ayse@test.local")


async def test_modul_kapaliysa_404(world: World) -> None:
    assert (await world.get("/documents")).status_code == 404


async def test_yukle_listele_indir(arsiv: World) -> None:
    w = arsiv
    created = await upload(w, valid_until="2026-12-31", retention_years="10")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["message"] == "Yönetim planı arşive eklendi."
    data = body["data"]
    assert {k: data[k] for k in ("title", "kind", "scope", "scope_name", "confidentiality")} == {
        "title": "Yönetim planı", "kind": "plan", "scope": "site", "scope_name": None,
        "confidentiality": "public",
    }  # fmt: skip
    assert (data["version"], data["file_name"], data["size"]) == (1, "plan.pdf", len(PDF))
    assert data["content_type"] == "application/pdf"
    assert data["sha256"] == FileStore.digest(PDF)
    assert data["uploaded_by"] == "Kerem YILDIRIM"
    assert (data["valid_until"], data["days_left"], data["warning"]) == ("2026-12-31", 194, None)
    assert data["retention_years"] == 10
    assert "taran" not in created.text  # virüs taraması yok; "tarandı" denmez

    listed = (await w.get("/documents")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == data["id"]
    assert len(listed["items"][0]["versions"]) == 1

    file = await w.get(f"/documents/{data['id']}/versions/1/download")
    assert file.status_code == 200
    assert file.content == PDF
    assert file.headers["content-type"] == "application/pdf"
    assert "plan.pdf" in file.headers["content-disposition"]
    assert (await w.get(f"/documents/{data['id']}/versions/9/download")).status_code == 404
    with site_scope(w.site_id):
        async with w.factory() as session:
            downloads = await session.scalar(
                select(AuditLog.id).where(
                    AuditLog.entity == "documents", AuditLog.action == "download"
                )
            )
    assert downloads is not None


async def test_dosya_icerikle_dogrulanir(arsiv: World) -> None:
    w = arsiv
    fake = await upload(w, file=("sozlesme.pdf", b"MZ\x90\x00 bu bir exe"))
    assert fake.status_code == 422
    assert fake.json()["error"]["fields"] == {
        "file": "Yalnız PDF, PNG, JPEG ya da WebP (dosya içeriği doğrulandı)."
    }
    big = await upload(w, file=("buyuk.pdf", PDF + b"0" * (10 * 1024 * 1024)))
    assert big.status_code == 422
    assert big.json()["error"]["fields"] == {"file": "Dosya en fazla 10 MB."}
    # uzantı yanlış ama içerik PNG: içerik geçerli, ad düzeltilir
    png = await upload(w, title="Asansör etiketi", kind="elevator", file=("etiket.pdf", PNG))
    assert png.status_code == 201, png.text
    assert (png.json()["data"]["file_name"], png.json()["data"]["content_type"]) == (
        "etiket.png",
        "image/png",
    )
    # yol ayıracı ve geçersiz karakterler temizlenir
    odd = await upload(w, title="Tutanak", kind="minutes", file=("../../gizli:tutanak.pdf", PDF))
    assert odd.json()["data"]["file_name"] == "gizli_tutanak.pdf"


async def test_alan_dogrulamasi(arsiv: World) -> None:
    w = arsiv
    response = await upload(
        w, title="ab", kind="tapu", scope="unit", confidentiality="secretish",
        valid_until="31.12.2026", retention_years="0",
    )  # fmt: skip
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {
        "valid_until": "Tarih seçin.",
        "title": "Belge adını yazın.",
        "kind": "Tür seçin.",
        "scope": "Blok ya da bölüm seçin.",
        "confidentiality": "Gizlilik seçin.",
        "retention_years": "Saklama süresi 1–100 yıl arasında olmalı.",
    }
    unit_private = await upload(w, confidentiality="unit")
    assert unit_private.json()["error"]["fields"] == {
        "confidentiality": "Bölüme özel belge bir bölüme bağlı olmalı."
    }
    missing = await upload(w, scope="block", scope_id=str(uuid.uuid4()))
    assert missing.json()["error"]["fields"] == {"scope": "Blok bulunamadı."}
    no_file = await w.api.post(
        w.url("/documents"),
        data={"title": "Kurallar", "kind": "rules", "scope": "site", "confidentiality": "public"},
        headers=w.headers,
    )
    assert no_file.json()["error"]["fields"] == {"file": "Dosya seçin."}


async def test_surumleme_ve_ayni_dosya(arsiv: World) -> None:
    w = arsiv
    first = (await upload(w, valid_until="2026-12-31")).json()["data"]
    again = await upload(w, title="  YÖNETİM   PLANI ", file=("plan-v2.pdf", PDF_2))
    assert again.status_code == 201, again.text
    assert again.json()["message"] == (
        "YÖNETİM PLANI için 2. sürüm kaydedildi; eski sürümler korunur."
    )
    data = again.json()["data"]
    assert data["id"] == first["id"]
    assert (data["version"], data["file_name"]) == (2, "plan-v2.pdf")
    assert data["valid_until"] == "2026-12-31"  # boş bırakılan bitiş korunur
    assert [(v["v"], v["file_name"]) for v in data["versions"]] == [
        (1, "plan.pdf"),
        (2, "plan-v2.pdf"),
    ]
    old = await w.get(f"/documents/{first['id']}/versions/1/download")
    assert old.content == PDF  # eski sürüm korunur

    same = await upload(w, file=("kopya.pdf", PDF_2))
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "same_file"
    assert same.json()["error"]["message"] == "Bu dosya son sürümle aynı."
    # eski sürümle aynı dosya son sürümle aynı değil → yeni sürüm (v3)
    assert (await upload(w, file=("plan.pdf", PDF))).json()["data"]["version"] == 3

    # aynı ad, başka kapsam → ayrı belge
    block = await upload(w, scope="block", scope_id=w.lookups["block"])
    assert block.status_code == 201
    assert block.json()["data"]["id"] != first["id"]
    assert block.json()["data"]["scope_name"] == "A"
    assert (await w.get("/documents")).json()["total"] == 2


async def test_surum_ve_acilis_kaydi_degismez(arsiv: World) -> None:
    w = arsiv
    await upload(w)
    with site_scope(w.site_id):
        async with w.factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text("UPDATE document_versions SET version = 5"))


async def test_gizli_belge(arsiv: World) -> None:
    w = arsiv
    board = await _staff(w, "yk@test.local", "Yönetim Kurulu Üyesi")
    auditor = await _staff(w, "denetci@test.local", "Denetçi")
    secret = (
        await upload(w, title="Avukat görüşü", kind="other", confidentiality="secret")
    ).json()["data"]
    management = (
        await upload(w, title="Teklif karşılaştırma", kind="other", confidentiality="management")
    ).json()["data"]

    async def titles(headers: dict[str, str]) -> set[str]:
        response = await w.api.get(w.url("/documents"), headers=headers)
        return {d["title"] for d in response.json()["items"]}

    assert await titles(board) == {"Teklif karşılaştırma"}
    assert await titles(auditor) == {"Avukat görüşü", "Teklif karşılaştırma"}
    path = f"/documents/{secret['id']}"
    for url in (f"{path}/versions/1/download", f"{path}/access-log"):
        hidden = await w.api.get(w.url(url), headers=board)
        assert hidden.status_code == 404
    assert (
        await w.api.get(w.url(f"/documents/{management['id']}/versions/1/download"), headers=board)
    ).status_code == 200

    assert (await w.get(f"{path}/access-log")).json()["items"] == []
    await w.get(f"{path}/versions/1/download")
    await w.api.get(w.url(f"{path}/versions/1/download"), headers=auditor)
    log = (await w.get(f"{path}/access-log")).json()
    assert log["total"] == 2
    assert [(e["by"], e["v"]) for e in log["items"]] == [
        ("Denetçi KİŞİ", 1),
        ("Kerem YILDIRIM", 1),
    ]
    # gizli olmayan belge açılışı kayda geçmez
    await w.get(f"/documents/{management['id']}/versions/1/download")
    assert (await w.get(f"/documents/{management['id']}/access-log")).json()["total"] == 0


async def test_yetki(arsiv: World) -> None:
    w = arsiv
    security = await _staff(w, "guvenlik@test.local", "Güvenlik")
    board = await _staff(w, "yk@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/documents"), headers=security)).status_code == 403
    assert (await w.api.get(w.url("/documents"), headers=board)).status_code == 200
    assert (await upload(w, headers=board)).status_code == 403
    resident = await _resident(w)
    assert (await w.api.get(w.url("/documents"), headers=resident)).status_code == 403
    # personel sakin ucunu kullanamaz
    assert (await w.get("/resident/documents")).status_code == 403


async def test_sakin_belgeleri(arsiv: World) -> None:
    w = arsiv
    other_unit = (await w.post("/units", {"block_id": w.lookups["block"], "number": "2"})).json()[
        "data"
    ]["id"]
    other_block = (await w.post("/blocks", {"name": "B"})).json()["data"]["id"]
    await upload(w, title="Site kuralları", kind="rules")
    await upload(w, title="A blok asansör", kind="elevator", scope="block",
                 scope_id=w.lookups["block"])  # fmt: skip
    await upload(w, title="B blok asansör", kind="elevator", scope="block", scope_id=other_block)
    await upload(w, title="Tadilat izni", kind="other", scope="unit", scope_id=w.unit_id,
                 confidentiality="unit")  # fmt: skip
    hidden_unit = (
        await upload(
            w,
            title="Komşu tadilat",
            kind="other",
            scope="unit",
            scope_id=other_unit,
            confidentiality="unit",
        )
    ).json()["data"]
    await upload(w, title="Teklifler", kind="other", confidentiality="management")
    await upload(w, title="Avukat görüşü", kind="other", confidentiality="secret")

    headers = await _resident(w)
    listed = await w.api.get(w.url("/resident/documents"), headers=headers)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert {d["title"] for d in items} == {"Site kuralları", "A blok asansör", "Tadilat izni"}
    assert "sha256" not in items[0]
    assert "versions" not in items[0]
    mine = next(d for d in items if d["title"] == "Tadilat izni")
    assert mine["scope_name"] == "A-1"

    file = await w.api.get(w.url(f"/resident/documents/{mine['id']}/download"), headers=headers)
    assert (file.status_code, file.content) == (200, PDF)
    stranger = await w.api.get(
        w.url(f"/resident/documents/{hidden_unit['id']}/download"), headers=headers
    )
    assert stranger.status_code == 404


async def test_site_izolasyonu(arsiv: World) -> None:
    w = arsiv
    doc = (await upload(w)).json()["data"]
    other = await w.api.get(w.url("/documents", "yildiz-sitesi"), headers=w.headers)
    assert other.status_code == 200
    assert other.json()["total"] == 0
    cross = await w.api.get(
        w.url(f"/documents/{doc['id']}/versions/1/download", "yildiz-sitesi"), headers=w.headers
    )
    assert cross.status_code == 404
    # başka sitenin bloğuna bağlanamaz
    foreign_block = (
        await w.api.post(w.url("/blocks", "yildiz-sitesi"), json={"name": "Y"}, headers=w.headers)
    ).json()["data"]["id"]
    linked = await upload(w, scope="block", scope_id=foreign_block)
    assert linked.json()["error"]["fields"] == {"scope": "Blok bulunamadı."}
    # aynı ad diğer sitede ayrı belge (sürüm değil)
    there = await upload(w, slug="yildiz-sitesi")
    assert there.status_code == 201
    assert there.json()["data"]["version"] == 1


async def test_gecerlilik_uyarisi(arsiv: World) -> None:
    w = arsiv
    auditor_id, _ = await _staff_user(w, "denetci@test.local", "Denetçi")  # yönetici değil
    policy = (
        await upload(w, title="Sigorta poliçesi", kind="insurance", valid_until="2026-08-04")
    ).json()["data"]
    assert (policy["days_left"], policy["warning"]) == (45, "60")
    fire = (
        await upload(w, title="Yangın raporu", kind="fire_report", valid_until="2026-07-10")
    ).json()["data"]
    assert fire["warning"] == "30"
    expired = (
        await upload(w, title="Asansör etiketi", kind="elevator", valid_until="2026-06-01")
    ).json()["data"]
    assert (expired["days_left"], expired["warning"]) == (-19, "expired")
    await upload(w, title="Yönetim planı", kind="plan", valid_until="2027-06-01")
    await upload(w, title="Gizli poliçe ek", kind="insurance", confidentiality="secret",
                 valid_until="2026-07-01")  # fmt: skip

    expiring = (await w.get("/documents", params={"expiring": "1"})).json()["items"]
    assert {d["title"] for d in expiring} == {
        "Sigorta poliçesi", "Yangın raporu", "Asansör etiketi", "Gizli poliçe ek",
    }  # fmt: skip
    by_kind = (await w.get("/documents", params={"kind": "insurance", "q": "SİGORTA"})).json()
    assert [d["title"] for d in by_kind["items"]] == ["Sigorta poliçesi"]

    results = await svc.run_expiry_warnings(w.factory, today=date(2026, 6, 20))
    assert results == [(w.site_id, 4)]
    again = await svc.run_expiry_warnings(w.factory, today=date(2026, 6, 20))
    assert again == [(w.site_id, 0)]  # tekrar güvenli
    with site_scope(w.site_id):
        async with w.factory() as session:
            rows = list(
                await session.execute(
                    select(Notification.kind, Notification.title, Notification.link_id)
                )
            )
    titles = sorted(t for _, t, _ in rows)
    assert titles == [
        "Asansör etiketi: geçerlilik süresi doldu; yenisini yükleyin.",
        "Gizli poliçe ek: geçerliliğin bitmesine 11 gün kaldı; yenilemeyi planlayın.",
        "Sigorta poliçesi: geçerliliğin bitmesine 45 gün kaldı; yenilemeyi planlayın.",
        "Yangın raporu: geçerliliğin bitmesine 20 gün kaldı; yenilemeyi planlayın.",
    ]
    assert {k for k, _, _ in rows} == {"document.expiring", "document.expired"}
    assert uuid.UUID(policy["id"]) in {i for _, _, i in rows}
    with site_scope(w.site_id):
        async with w.factory() as session:
            recipients = set(await session.scalars(select(Notification.user_id)))
    assert auditor_id not in recipients
    assert len(recipients) == 1  # yalnız belge yöneticisi

    # 30 güne inince ikinci uyarı; yeni sürümle bitiş değişince uyarılar yeniden başlar
    later = await svc.run_expiry_warnings(w.factory, today=date(2026, 7, 6))
    assert later == [(w.site_id, 2)]  # poliçe 29 gün (30 eşiği) + gizli ek bitti
    renewed = await upload(w, title="Sigorta poliçesi", kind="insurance",
                           valid_until="2026-08-30", file=("police-2.pdf", PDF_2))  # fmt: skip
    assert renewed.json()["data"]["version"] == 2
    assert await svc.run_expiry_warnings(w.factory, today=date(2026, 7, 6)) == [(w.site_id, 1)]


async def test_komut_satiri(
    arsiv: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-document-expiry"]) == 0
    finally:
        get_settings.cache_clear()
    assert "sitede belge geçerlilik uyarısı çalıştı" in capsys.readouterr().out
