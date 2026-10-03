"""Otomatik aylık tahakkuk — frontend servis isteği 04, issue #27 (gerçek PostgreSQL).

Kurgu `finance_world`: A-1 (A1-O), yıllık 12.000 aidat (aylık 1.000); bugün 20.06.2026.
"""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from site_yonetim import cli
from site_yonetim.core.config import get_settings
from site_yonetim.services import charge_schedule as svc
from site_yonetim.services import charging
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers

DEFAULT = {
    "enabled": False,
    "charge_day": 1,
    "due_days": 14,
    "notify_on_run": True,
    "next_run_on": None,
    "last_run": None,
}


async def schedule(w: World, slug: str = "aksu-konaklari") -> dict[str, Any]:
    response = await w.api.get(w.url("/charge-schedule", slug), headers=w.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def put(w: World, **overrides: Any) -> Any:
    body = {"enabled": True, "charge_day": 1, "due_days": 14, "notify_on_run": True, **overrides}
    return await w.api.put(w.url("/charge-schedule"), json=body, headers=w.headers)


async def run(w: World, day: date) -> list[svc.SiteResult]:
    w.set_today(day)
    return await svc.run_all(
        w.factory, today=day, now=datetime(day.year, day.month, day.day, 3, tzinfo=UTC)
    )


async def test_ayar_varsayilan_kayit_ve_mesajlar(world: World) -> None:
    assert await schedule(world) == DEFAULT

    opened = await put(world, charge_day=25)
    assert opened.status_code == 200, opened.text
    assert opened.json()["message"] == "Otomatik tahakkuk açıldı; her ayın 25. günü kesilecek."
    assert opened.json()["data"]["next_run_on"] == "2026-06-25"

    updated = await put(world, charge_day=1, due_days=10)
    assert updated.json()["message"] == "Otomatik tahakkuk güncellendi; her ayın 1. günü kesilecek."
    # 20 Haziran'da açılan takvim Haziran'ın 1'ini geriye dönük kesmez
    assert updated.json()["data"]["next_run_on"] == "2026-07-01"

    closed = await put(world, enabled=False)
    assert closed.json()["message"] == "Otomatik tahakkuk kapatıldı."
    assert closed.json()["data"]["next_run_on"] is None
    assert await schedule(world, "yildiz-sitesi") == DEFAULT  # başka sitenin ayarı ayrı

    audit = (await world.get("/audit", params={"entity": "charge_schedules"})).json()
    assert [r["action"] for r in audit["items"]] == ["update", "update", "create"]


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"charge_day": 0}, "charge_day", "Gün 1–28 arasında olmalı (her ayda olsun diye)."),
        ({"charge_day": 29}, "charge_day", "Gün 1–28 arasında olmalı (her ayda olsun diye)."),
        ({"due_days": 61}, "due_days", "Vade 0–60 gün olmalı."),
        ({"due_days": -1}, "due_days", "Vade 0–60 gün olmalı."),
    ],
)
async def test_ayar_dogrulama(
    world: World, overrides: dict[str, int], field: str, message: str
) -> None:
    response = await put(world, **overrides)
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {field: message}
    assert await schedule(world) == DEFAULT


async def test_gece_isi_keser_ve_ikinci_kez_kesmez(world: World) -> None:
    await finalized_plan(world)
    await put(world, charge_day=1, due_days=14)
    assert await run(world, date(2026, 6, 20)) == []  # kesim günü bu ay geçti, açılmadan önce

    [result] = await run(world, date(2026, 7, 1))
    assert result.site_id == world.site_id
    assert result.outcome.status == "posted"
    assert await world.balance() == Decimal("1000.00")
    [charge_run] = (await world.get("/charge-runs")).json()["items"]
    assert (charge_run["charge_date"], charge_run["due_date"]) == ("2026-07-01", "2026-07-15")

    body = await schedule(world)
    assert body["last_run"]["status"] == "posted"
    assert body["last_run"]["period"] == "07/2026"
    assert body["last_run"]["run_id"] == charge_run["id"]
    assert body["next_run_on"] == "2026-08-01"

    # iş aynı gün tekrar çalışırsa: atlanır, yeni tahakkuk ve yeni kayıt yok
    [again] = await run(world, date(2026, 7, 1))
    assert (again.outcome.status, again.outcome.message) == (
        "skipped",
        "07/2026 için otomatik tahakkuk zaten çalıştı.",
    )
    assert await world.balance() == Decimal("1000.00")

    audit = (await world.get("/audit", params={"entity": "charge_runs"})).json()["items"]
    assert audit[0]["actor_name"] == "Otomatik tahakkuk"


async def test_sunucu_kesim_gunu_kapaliysa_ertesi_gun_yetisir(world: World) -> None:
    await finalized_plan(world)
    await put(world, charge_day=1)
    [result] = await run(world, date(2026, 7, 3))
    assert result.outcome.status == "posted"
    [charge_run] = (await world.get("/charge-runs")).json()["items"]
    assert charge_run["charge_date"] == "2026-07-01"  # tarih kesim günü, çalıştığı gün değil


async def test_elle_kesilmis_donem_atlanir(world: World) -> None:
    await finalized_plan(world)
    await put(world, charge_day=1)
    world.set_today(date(2026, 7, 1))
    assert (await post_run(world, "2026-07-01")).status_code == 201
    [result] = await run(world, date(2026, 7, 1))
    assert result.outcome.status == "skipped"
    assert "07/2026" in (result.outcome.message or "")
    assert await world.balance() == Decimal("1000.00")
    assert (await schedule(world))["last_run"]["status"] == "skipped"


async def test_proje_yoksa_ya_da_uyari_varsa_kesmez(world: World) -> None:
    await put(world, charge_day=1)
    [no_plan] = await run(world, date(2026, 7, 1))
    assert (no_plan.outcome.status, no_plan.outcome.message) == (
        "skipped",
        "Tahakkuk kesmek için önce işletme projesi kesinleşmeli.",
    )

    # Ağustos: proje var ama ödeyeni olmayan bir bölüm → önizlemede uyarı → kesilmez
    world.set_today(date(2026, 7, 2))
    await finalized_plan(world)
    unit = await world.post("/units", {"block_id": world.lookups["block"], "number": "2"})
    assert unit.status_code == 201, unit.text
    [warned] = await run(world, date(2026, 8, 1))
    assert warned.outcome.status == "skipped"
    assert (warned.outcome.message or "").startswith("Önizlemede 1 uyarı var; tahakkuk kesilmedi")
    assert await world.balance() == Decimal("0.00")
    assert (await world.get("/charge-runs")).json()["total"] == 0


async def test_beklenmeyen_hata_ertesi_gun_yeniden_denenir(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await finalized_plan(world)
    await put(world, charge_day=1)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("veritabanı bağlantısı koptu")

    original = charging.post
    monkeypatch.setattr(charging, "post", broken)
    [failed] = await run(world, date(2026, 7, 1))
    assert failed.outcome.status == "failed"
    assert (await schedule(world))["last_run"]["status"] == "failed"
    assert (await schedule(world))["next_run_on"] == "2026-07-01"  # bekliyor

    monkeypatch.setattr(charging, "post", original)
    [retried] = await run(world, date(2026, 7, 2))
    assert retried.outcome.status == "posted"
    assert await world.balance() == Decimal("1000.00")
    assert (await schedule(world))["last_run"]["status"] == "posted"


async def test_yetki(world: World) -> None:
    user = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    auditor = await login_headers(world.api, "denetci@test.local")
    assert (await world.api.get(world.url("/charge-schedule"), headers=auditor)).status_code == 200
    body = {"enabled": True, "charge_day": 1, "due_days": 14, "notify_on_run": True}
    put_response = await world.api.put(world.url("/charge-schedule"), json=body, headers=auditor)
    assert put_response.status_code == 403
    assert await schedule(world) == DEFAULT


async def test_komut_satiri(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-charge-schedules"]) == 0
    finally:
        get_settings.cache_clear()
    assert "sitede otomatik tahakkuk çalıştı" in capsys.readouterr().out
