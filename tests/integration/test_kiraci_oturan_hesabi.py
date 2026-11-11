"""Kiracı eklenince malikin oturan (`-O`) hesabının kapanışı — docs/03 §5, docs/04 §4.5 (K9).

Kurgu `finance_world`: A-1 maliki Ayşe Yılmaz (2020'den beri; A1-M, A1-O), yıllık 12.000 aidat
(aylık 1.000), gün bazlı kıst; bugün 20.06.2026. Kiracı 15 Haziran'da gelir: Haziran'ın ilk 14
günü malikin `-O` hesabına yazılabilmeli, hesap ancak 1 Temmuz'dan sonra (bakiyesi sıfırsa)
günlük yapı işiyle kapanır.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, LedgerAccount
from site_yonetim.services import structure_jobs
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.test_finance_ops import pay


async def _tenant(w: World, start: str) -> dict[str, Any]:
    response = await w.post(
        f"/units/{w.unit_id}/parties",
        {
            "role": "tenant",
            "start_date": start,
            "person": {"first_name": "Mehmet", "last_name": "Kaya"},
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _owner_occupant(w: World) -> LedgerAccount:
    with site_scope(w.site_id):
        async with w.factory() as session:
            account = await session.get(LedgerAccount, uuid.UUID(w.account_id))
    assert account is not None
    return account


async def _run_jobs(w: World, day: date) -> list[structure_jobs.SiteResult]:
    return await structure_jobs.run_all(
        w.factory, today=day, now=datetime(day.year, day.month, day.day, 3, tzinfo=UTC)
    )


def _by_code(preview: dict[str, Any]) -> dict[str, tuple[str, int | None]]:
    return {c["reference_code"]: (c["amount"], c["days"]) for c in preview["charges"]["items"]}


async def test_ay_ortasi_kiraci_malik_payi_yazilir_sonraki_ay_hesap_kapanir(world: World) -> None:
    w = world
    await finalized_plan(w)

    added = await _tenant(w, "2026-06-15")

    data = added["data"]
    assert data["closed_accounts"] == []
    [pending] = data["kept_open_accounts"]
    assert (pending["reference_code"], pending["is_closed"]) == ("A1-O", False)
    assert (pending["pending_close"], pending["close_from"], pending["has_balance"]) == (
        True,
        "2026-07-01",
        False,
    )
    assert "A1-O 01.07.2026 tarihinden itibaren kapanacak" in added["message"]

    # Haziran kıst: ilk 14 gün malikin -O hesabına, kalan 16 gün kiracının -K hesabına.
    june = (await w.get("/charge-runs/preview", params={"charge_date": "2026-06-01"})).json()
    assert june["warnings"] == []
    assert _by_code(june) == {"A1-O": ("466.67", 14), "A1-K": ("533.33", 16)}

    # Ay bitmeden günlük iş kapatmaz; 1 Temmuz'da kapatır.
    assert await _run_jobs(w, date(2026, 6, 30)) == []
    assert (await _owner_occupant(w)).is_closed is False
    results = await _run_jobs(w, date(2026, 7, 1))
    assert [(r.site_id, r.closed_occupants) for r in results] == [(w.site_id, 1)]
    assert (await _owner_occupant(w)).is_closed is True

    with site_scope(w.site_id):
        async with w.factory() as session:
            audit = list(
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.entity == "ledger_accounts",
                        AuditLog.entity_id == uuid.UUID(w.account_id),
                    )
                )
            )
    assert [(a.after, a.actor_type) for a in audit] == [
        ({"reference_code": "A1-O", "kind": "occupant", "is_closed": True}, "system")
    ]
    # Temmuz'un tamamı kiracıya; kapalı hesap için uyarı yok.
    w.set_today(date(2026, 7, 1))
    july = (await w.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})).json()
    assert july["warnings"] == []
    assert _by_code(july) == {"A1-K": ("1000.00", None)}
    # Ertesi gün iş tekrar çalışınca yapacak bir şey yok.
    assert await _run_jobs(w, date(2026, 7, 2)) == []


async def test_bakiyeli_malik_oturan_hesabi_acik_kalir_odeyince_kapanir(world: World) -> None:
    w = world
    await finalized_plan(w)
    await _tenant(w, "2026-06-15")
    assert (await post_run(w, "2026-06-01")).status_code == 201  # A1-O'ya 466,67 borç

    assert await _run_jobs(w, date(2026, 7, 1)) == []  # bakiye var: açık kalır
    assert (await _owner_occupant(w)).is_closed is False
    assert await _run_jobs(w, date(2026, 7, 2)) == []  # her gün yeniden dener

    assert (await pay(w, "466.67")).status_code == 201
    results = await _run_jobs(w, date(2026, 7, 3))
    assert [r.closed_occupants for r in results] == [1]
    assert (await _owner_occupant(w)).is_closed is True


async def test_gecmis_ayda_baslayan_kiraci_eklenince_hemen_kapanir(world: World) -> None:
    """Koşullar zaten sağlanıyorsa (başlangıç ayı geçti, bakiye sıfır) iş beklenmez."""
    w = world

    added = await _tenant(w, "2026-05-10")

    data = added["data"]
    assert [a["reference_code"] for a in data["closed_accounts"]] == ["A1-O"]
    assert data["kept_open_accounts"] == []
    assert "malikin oturan hesabı kapatıldı (A1-O)" in added["message"]
    assert (await _owner_occupant(w)).is_closed is True


async def test_bakiyeli_ve_baslangic_ayi_gecmis_hesap_bakiye_sifirlaninca_kapanacak(
    world: World,
) -> None:
    w = world
    await finalized_plan(w)
    assert (await post_run(w, "2026-06-01")).status_code == 201  # A1-O'ya 1.000 borç

    added = await _tenant(w, "2026-05-10")

    [pending] = added["data"]["kept_open_accounts"]
    assert (pending["pending_close"], pending["close_from"], pending["has_balance"]) == (
        True,
        "2026-06-01",
        True,
    )
    assert "A1-O bakiyesi sıfırlanınca kapanacak" in added["message"]
    assert (await _owner_occupant(w)).is_closed is False


async def test_kiraci_kapanistan_once_cikarsa_hesap_kapanmaz(world: World) -> None:
    w = world
    party = (await _tenant(w, "2026-06-15"))["data"]["party"]
    url = f"/units/{w.unit_id}/parties/{party['id']}/end"
    ended = await w.post(url, {"end_date": "2026-06-25"})
    assert ended.status_code == 200, ended.text

    assert await _run_jobs(w, date(2026, 7, 1)) == []
    assert (await _owner_occupant(w)).is_closed is False
