"""Tekrarlanan gider — frontend servis isteği 07, issue #36 (gerçek PostgreSQL).

Kurgu `finance_world`: bugün 20.06.2026; sitenin ilk gider kategorisi ve "Banka Hesabı".
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

import httpx2
import pytest

from site_yonetim import cli
from site_yonetim.core.config import get_settings
from site_yonetim.services import expenses
from site_yonetim.services import recurring_expenses as svc
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def bank(w: World) -> str:
    accounts = (await w.get("/cash-accounts")).json()["items"]
    return str(next(a["id"] for a in accounts if a["name"] == "Banka Hesabı"))


def body(w: World, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": "Asansör bakım sözleşmesi",
        "expense_category_id": w.lookups["category"],
        "amount": "19440.00",
        "vendor": "Asansör Servis A.Ş.",
        "day_of_month": 25,
        "auto_pay": False,
        "cash_account_id": None,
        "is_active": True,
    }
    data.update(overrides)
    return data


async def create(w: World, **overrides: Any) -> httpx2.Response:
    return await w.post("/recurring-expenses", body(w, **overrides))


async def patch(w: World, item_id: str, changes: dict[str, Any]) -> httpx2.Response:
    return await w.api.patch(
        w.url(f"/recurring-expenses/{item_id}"), json=changes, headers=w.headers
    )


async def run(w: World, day: date) -> list[tuple[uuid.UUID, svc.Outcome]]:
    w.set_today(day)
    now = datetime(day.year, day.month, day.day, 3, tzinfo=UTC)
    return await svc.run_all(w.factory, today=day, now=now)


async def listing(w: World) -> list[dict[str, Any]]:
    response = await w.get("/recurring-expenses")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()
    return items


async def test_tanim_ve_siradaki_calisma(world: World) -> None:
    assert await listing(world) == []
    created = await create(world)
    assert created.status_code == 201, created.text
    assert created.json()["message"] == '"Asansör bakım sözleşmesi" her ayın 25. günü kaydedilecek.'
    data = created.json()["data"]
    assert (data["amount"], data["next_run_on"], data["last_created_on"]) == (
        "19440.00", "2026-06-25", None
    )  # fmt: skip
    # 20 Haziran'da tanımlanan, günü 1 olan gider Haziran'ın 1'ini geriye dönük yazmaz
    early = (await create(world, description="Kapıcı maaşı", day_of_month=1)).json()["data"]
    assert early["next_run_on"] == "2026-07-01"
    assert [i["description"] for i in await listing(world)] == [
        "Kapıcı maaşı",
        "Asansör bakım sözleşmesi",
    ]


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"description": "  "}, "description", "Açıklama zorunlu."),
        ({"amount": "0.00"}, "amount", "Tutar sıfırdan büyük olmalı."),
        ({"day_of_month": 29}, "day_of_month", "Gün 1–28 arasında olmalı (her ayda olsun diye)."),
        ({"auto_pay": True}, "cash_account_id", "Otomatik ödeme için hesap seçin."),
        ({"expense_category_id": str(uuid.uuid4())}, "expense_category_id", "Kategori seçin."),
    ],
)
async def test_dogrulama(world: World, overrides: dict[str, Any], field: str, message: str) -> None:
    response = await create(world, **overrides)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["fields"] == {field: message}


async def test_gece_isi_gider_yazar_ikinci_kez_yazmaz(world: World) -> None:
    plain = (await create(world)).json()["data"]
    account = await bank(world)
    extra = {"amount": "5000.00", "auto_pay": True, "cash_account_id": account}
    paid = (await create(world, description="Temizlik firması", **extra)).json()["data"]
    assert await run(world, date(2026, 6, 24)) == []  # gün gelmedi

    results = await run(world, date(2026, 6, 25))
    assert sorted(o.status for _, o in results) == ["created", "created"]
    listed = (await world.get("/expenses", params={"year": 2026})).json()["items"]
    by_description = {e["description"]: e for e in listed}
    assert by_description["Asansör bakım sözleşmesi — 06/2026"]["paid_on"] is None
    assert by_description["Temizlik firması — 06/2026"]["paid_on"] == "2026-06-25"
    balances = {
        a["name"]: a["balance"] for a in (await world.get("/cash-accounts")).json()["items"]
    }
    assert balances["Banka Hesabı"] == "-5000.00"

    assert await run(world, date(2026, 6, 25)) == []  # iş ikinci kez: yeni gider yok
    assert await run(world, date(2026, 6, 28)) == []
    assert (await world.get("/expenses", params={"year": 2026})).json()["total"] == 2

    items = {i["id"]: i for i in await listing(world)}
    assert (items[plain["id"]]["last_created_on"], items[plain["id"]]["next_run_on"]) == (
        "2026-06-25", "2026-07-25"
    )  # fmt: skip
    assert items[paid["id"]]["last_created_on"] == "2026-06-25"
    audit = (await world.get("/audit", params={"entity": "expenses"})).json()["items"]
    assert {a["actor_name"] for a in audit} == {"Tekrarlanan gider"}


async def test_kacirilan_gun_ertesi_gun_yetisir(world: World) -> None:
    await create(world)
    [(_, outcome)] = await run(world, date(2026, 6, 27))
    assert outcome.status == "created"
    [expense] = (await world.get("/expenses", params={"year": 2026})).json()["items"]
    assert expense["date"] == "2026-06-25"  # tarih tanımdaki gün


async def test_durdur_baslat_ve_kaldir(world: World) -> None:
    item = (await create(world)).json()["data"]
    stopped = await patch(world, item["id"], {"is_active": False})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["message"] == '"Asansör bakım sözleşmesi" durduruldu.'
    assert stopped.json()["data"]["next_run_on"] is None
    assert await run(world, date(2026, 6, 25)) == []

    world.set_today(date(2026, 7, 28))
    restarted = await patch(world, item["id"], {"is_active": True, "amount": "20000.00"})
    assert restarted.json()["message"] == (
        '"Asansör bakım sözleşmesi" yeniden başlatıldı; her ayın 25. günü kaydedilecek.'
    )
    # durdurulduğu dönem geriye dönük yazılmaz: Temmuz'un 25'i geçti → Ağustos
    assert restarted.json()["data"]["next_run_on"] == "2026-08-25"
    assert restarted.json()["data"]["amount"] == "20000.00"
    assert await run(world, date(2026, 7, 28)) == []

    renamed = await patch(world, item["id"], {"description": "Asansör bakımı"})
    assert renamed.json()["message"] == '"Asansör bakımı" güncellendi.'

    [(_, outcome)] = await run(world, date(2026, 8, 25))
    assert outcome.status == "created"
    removed = await world.api.delete(
        world.url(f"/recurring-expenses/{item['id']}"), headers=world.headers
    )
    assert removed.status_code == 200
    assert removed.json()["message"] == (
        '"Asansör bakımı" kaldırıldı. Daha önce oluşturulan giderler yerinde kalır.'
    )
    assert await listing(world) == []
    assert await run(world, date(2026, 9, 25)) == []
    assert (await world.get("/expenses", params={"year": 2026})).json()["total"] == 1
    assert (await patch(world, item["id"], {"is_active": True})).status_code == 404


async def test_beklenmeyen_hata_ertesi_gun_yeniden_denenir(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create(world)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("disk dolu")

    original = expenses.create
    monkeypatch.setattr(expenses, "create", broken)
    [(_, failed)] = await run(world, date(2026, 6, 25))
    assert failed.status == "failed"
    monkeypatch.setattr(expenses, "create", original)
    [(_, retried)] = await run(world, date(2026, 6, 26))
    assert retried.status == "created"


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    item = (await create(world)).json()["data"]
    user = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    auditor = await login_headers(world.api, "denetci@test.local")
    url = world.url("/recurring-expenses")
    assert (await world.api.get(url, headers=auditor)).status_code == 200
    assert (await world.api.post(url, json=body(world), headers=auditor)).status_code == 403

    other = world.url("/recurring-expenses", "yildiz-sitesi")
    assert (await world.api.get(other, headers=world.headers)).json() == []
    foreign = await world.api.patch(
        f"{other}/{item['id']}", json={"is_active": False}, headers=world.headers
    )
    assert foreign.status_code == 404
    # Yıldız'da Aksu'nun gider kategorisi yok
    wrong = await world.api.post(other, json=body(world), headers=world.headers)
    assert wrong.json()["error"]["fields"] == {"expense_category_id": "Kategori seçin."}


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
        assert await asyncio.to_thread(cli.main, ["run-recurring-expenses"]) == 0
        assert await asyncio.to_thread(cli.main, ["purge-imports"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "tekrarlanan gider işlendi" in out
    assert "banka ekstresi önizlemesi silindi" in out
