"""Gece bakiye mutabakatı — docs/08 §2 (gerçek PostgreSQL).

Kurgu `finance_world`: Aksu'da A-1 için 1.000 tahakkuk, 400 tahsilat; Banka'ya 10.000 açılış.
Özet tablolar elle bozulur (tablo sahibi bağlantısıyla), mutabakat yakalar ve onarır.
"""

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from site_yonetim import cli
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.cash import CashSource
from site_yonetim.models import CashAccount
from site_yonetim.services import cash as cash_svc
from site_yonetim.services import reconciliation
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World, finalized_plan, post_run


@pytest.fixture
async def books(world: World) -> World:
    await finalized_plan(world)
    await post_run(world)
    paid = await world.post(
        "/payments",
        {
            "ledger_account_id": world.account_id,
            "amount": "400.00",
            "date": "2026-06-18",
            "method": "cash",
        },
    )
    assert paid.status_code == 201, paid.text
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            bank = await session.scalar(
                select(CashAccount).where(CashAccount.name == "Banka Hesabı")
            )
            assert bank is not None
            await cash_svc.add_movement(
                session, bank, day=date(2026, 6, 20) - timedelta(days=30),
                inflow=Decimal(10000), description="Açılış", source=CashSource.OPENING,
            )  # fmt: skip
    return world


async def _tamper(engine: AsyncEngine, w: World, *statements: str) -> None:
    """Özet tabloyu elle boz (`:site` = Aksu, `:other` = Yıldız)."""
    params = {"site": w.site_id, "other": w.other_site_id, "row": uuid.uuid4()}
    async with engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.all_sites', 'on', true)"))
        for statement in statements:
            used = {k: v for k, v in params.items() if f":{k}" in statement}
            await connection.execute(text(statement), used)


async def test_tutarli_defterde_fark_yok(books: World) -> None:
    assert await reconciliation.find_mismatches(books.factory) == []


async def test_bozulan_ozetler_yakalanir_ve_onarilir(
    books: World, admin_engine: AsyncEngine
) -> None:
    await _tamper(
        admin_engine,
        books,
        "UPDATE account_balances SET balance = balance + 50 WHERE site_id = :site",
        "UPDATE cash_balances SET inflow_total = 1, balance = 1"
        " WHERE site_id = :site AND inflow_total > 0",
        "UPDATE site_finance_summary SET collected = 0 WHERE site_id = :site",
    )
    found = await reconciliation.find_mismatches(books.factory)

    assert {m.site_id for m in found} == {books.site_id}  # Yıldız etkilenmedi
    account = next(m for m in found if m.kind == "account")
    assert (account.key, account.field) == (books.account_id, "balance")
    assert (account.expected, account.actual) == (Decimal("600.00"), Decimal("650.00"))
    cash = {m.field: (m.expected, m.actual) for m in found if m.kind == "cash"}
    assert cash["balance"][1] == Decimal(1)
    summary = next(m for m in found if m.kind == "summary")
    assert (summary.key, summary.field) == ("2026-06", "collected")
    assert (summary.expected, summary.actual) == (Decimal("400.00"), Decimal("0.00"))

    await reconciliation.repair(books.factory, {m.site_id for m in found})

    assert await reconciliation.find_mismatches(books.factory) == []
    assert await books.balance() == Decimal("600.00")


async def test_eksik_ve_fazla_ozet_satiri(books: World, admin_engine: AsyncEngine) -> None:
    await _tamper(
        admin_engine,
        books,
        "DELETE FROM account_balances WHERE site_id = :site",
        "INSERT INTO site_finance_summary (id, site_id, year, month, charged, collected) "
        "VALUES (:row, :other, 2026, 1, 99, 0)",
    )
    found = await reconciliation.find_mismatches(books.factory)

    missing = next(m for m in found if m.kind == "account" and m.field == "debit_total")
    assert (missing.expected, missing.actual) == (Decimal("1000.00"), Decimal(0))
    extra = next(m for m in found if m.kind == "summary")
    assert (extra.site_id, extra.key, extra.expected, extra.actual) == (
        books.other_site_id, "2026-01", Decimal(0), Decimal("99.00")
    )  # fmt: skip

    await reconciliation.repair(books.factory, {m.site_id for m in found})
    assert await reconciliation.find_mismatches(books.factory) == []


async def test_komut_satiri_alarm_ve_onarim(
    books: World,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    await _tamper(
        admin_engine, books, "UPDATE account_balances SET balance = 0 WHERE site_id = :site"
    )
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["reconcile"]) == 1
        assert await asyncio.to_thread(cli.main, ["reconcile", "--fix"]) == 0
        assert await asyncio.to_thread(cli.main, ["reconcile"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "1 fark bulundu" in out
    assert "onarıldı" in out
    assert "Mutabakat tuttu" in out
