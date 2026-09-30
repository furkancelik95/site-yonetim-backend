"""Excel'den bölüm ve kişi aktarımı uçları — docs/11, docs/06 §2.5 (gerçek PostgreSQL, RLS).

Bitti ölçütü: önizleme hiçbir şey yazmaz; onay tek transaction ile yazar, kayıtlı bölümü
atlar; aktarım yalnız yükleyene, o sitede ve bir kez açıktır; başka site etkilenmez.
"""

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx2
import openpyxl
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_today
from site_yonetim.api.v1.imports import get_import_store
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.imports.unit_validator import Column
from site_yonetim.models import Block, LedgerAccount, Person, Plan, Site, Unit, UnitParty, UnitType
from site_yonetim.services import imports as import_svc
from site_yonetim.services.imports import ImportStore
from site_yonetim.services.provisioning import provision_site
from tests.integration.conftest import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADER = [c.value for c in Column]


@dataclass(frozen=True)
class World:
    a: uuid.UUID
    b: uuid.UUID
    headers: dict[str, str]  # iki sitede de Yönetici
    store: ImportStore


@pytest.fixture
async def world(
    api: httpx2.AsyncClient,
    api_app: FastAPI,
    session_factory: Factory,
    admin_engine: object,
    tmp_path: Path,
) -> World:
    api_app.dependency_overrides[get_today] = lambda: date(2026, 10, 1)
    store = ImportStore(tmp_path / "imports")
    api_app.dependency_overrides[get_import_store] = lambda: store
    site_ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            site_ids.append((await provision_site(session, name=name)).id)
    user = await create_user(session_factory, "yonetici@test.local")
    for site_id in site_ids:
        await add_site_membership(session_factory, site_id, user.id, "Yönetici")
    headers = await login_headers(api, "yonetici@test.local")
    return World(site_ids[0], site_ids[1], headers, store)


def xlsx(*rows: Sequence[object], header: Sequence[object] = HEADER) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = "Daireler"
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def unit_row(block: str, number: str, owner: str = "ayşe yılmaz", **extra: object) -> list[object]:
    first, last = owner.split()
    values: dict[Column, object] = {
        Column.BLOCK: block,
        Column.NUMBER: number,
        Column.OWNER_FIRST: first,
        Column.OWNER_LAST: last,
    }
    values.update({Column[key.upper()]: value for key, value in extra.items()})
    return [values.get(c) for c in Column]


def url(slug: str, path: str = "") -> str:
    return f"/api/v1/sites/{slug}/imports/units{path}"


async def upload(
    api: httpx2.AsyncClient,
    w: World,
    data: bytes,
    *,
    slug: str = "aksu-konaklari",
    name: str = "daireler.xlsx",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    return await api.post(
        url(slug), files={"file": (name, data, XLSX)}, headers=headers or w.headers
    )


async def confirm(
    api: httpx2.AsyncClient,
    w: World,
    import_id: str,
    *,
    slug: str = "aksu-konaklari",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    return await api.post(url(slug, f"/{import_id}/confirm"), headers=headers or w.headers)


async def counts(factory: Factory, site_id: uuid.UUID) -> dict[str, int]:
    with site_scope(site_id):
        async with factory() as session:
            return {
                model.__tablename__: len((await session.scalars(select(model.id))).all())
                for model in (Block, Unit, Person, UnitParty, LedgerAccount)
            }


async def create_unit_with_owner(
    api: httpx2.AsyncClient, w: World, block: str, number: str
) -> None:
    base = "/api/v1/sites/aksu-konaklari"
    blocks = (await api.get(f"{base}/blocks", headers=w.headers)).json()["items"]
    block_id = next((b["id"] for b in blocks if b["name"] == block), None)
    if block_id is None:
        response = await api.post(f"{base}/blocks", json={"name": block}, headers=w.headers)
        block_id = response.json()["data"]["id"]
    unit = await api.post(
        f"{base}/units", json={"block_id": block_id, "number": number}, headers=w.headers
    )
    party = await api.post(
        f"{base}/units/{unit.json()['data']['id']}/parties",
        json={
            "role": "owner",
            "start_date": "2026-01-01",
            "person": {"first_name": "Eski", "last_name": "Malik"},
        },
        headers=w.headers,
    )
    assert party.status_code == 201, party.text


# --- şablon -------------------------------------------------------------------------


async def test_template_download(api: httpx2.AsyncClient, world: World) -> None:
    response = await api.get(url("aksu-konaklari", "/template.xlsx"), headers=world.headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.content.startswith(b"PK\x03\x04")
    assert import_svc.read_and_validate(response.content).importable_count == 3


# --- bitti ölçütü -----------------------------------------------------------------


async def test_preview_writes_nothing_then_confirm_writes_all(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    before = await counts(session_factory, world.a)
    data = xlsx(
        unit_row("A", "1", owner_phone="0532 123 45 67", unit_type="2+1", gross_area="104,5"),
        unit_row("A", "2", "mehmet kaya", tenant_first="elif", tenant_last="demir"),
        unit_row("B", "Z1", "ali çelik", usage="Dükkan", unit_type="Dükkan"),
        unit_row("B", "Z2", "Ayşe2 Kaya"),  # hatalı: isimde rakam
    )

    preview = await upload(api, world, data)

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert await counts(session_factory, world.a) == before  # hiçbir kayıt yok
    assert (body["total_rows"], body["importable_count"], body["new_count"]) == (4, 3, 3)
    assert (body["error_count"], body["existing_count"]) == (1, 0)
    assert body["issues"] == [
        {
            "row_number": 5,
            "column": "Malik Ad",
            "message": "'Ayşe2': Malik Ad rakam içeremez.",
            "severity": "error",
        }
    ]
    first = body["rows"][0]
    assert first["display_name"] == "A-1"
    assert first["gross_area"] == "104.50"
    assert first["owner"] == {
        "first_name": "Ayşe",
        "last_name": "YILMAZ",
        "phone": "+905321234567",
        "email": None,
    }
    assert body["rows"][1]["tenant"]["last_name"] == "DEMİR"
    assert body["import_id"]
    assert body["expires_at"]

    result = await confirm(api, world, body["import_id"])

    assert result.status_code == 200, result.text
    assert result.json() == {
        "data": {
            "created_units": 3,
            "created_people": 4,
            "created_accounts": 6,  # A-1: M+O · A-2: M + kiracıya K · B-Z1: M+O
            "created_blocks": 2,
            "created_unit_types": 1,  # "2+1" site açılışında var; yalnız "Dükkan" yeni
            "skipped": [],
        },
        "message": "3 bölüm ve 4 kişi aktarıldı.",
    }
    assert list(world.store.root.glob("*/*/*")) == []  # geçici dosya silindi

    with site_scope(world.a):
        async with session_factory() as session:
            accounts = {
                a.reference_code: a.kind for a in await session.scalars(select(LedgerAccount))
            }
            parties = (await session.scalars(select(UnitParty))).all()
            types = {t.name: t.weight for t in await session.scalars(select(UnitType))}
            shop = await session.scalar(select(Unit).where(Unit.number == "Z1"))
    assert accounts == {
        "A1-M": "owner",
        "A1-O": "occupant",
        "A2-M": "owner",
        "A2-K": "occupant",
        "BZ1-M": "owner",
        "BZ1-O": "occupant",
    }
    assert {p.start_date for p in parties} == {date(2026, 1, 1)}  # yılın 1 Ocak'ı
    assert types["Dükkan"] == Decimal(1)  # yeni tip ağırlık 1
    assert shop is not None
    assert shop.usage == "commercial"
    assert await counts(session_factory, world.b) == {
        "blocks": 0,
        "units": 0,
        "persons": 0,
        "unit_parties": 0,
        "ledger_accounts": 0,
    }


async def test_existing_unit_is_skipped_not_overwritten(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    await create_unit_with_owner(api, world, "A", "1")
    # "a" mevcut "A" bloğudur (harf duyarsız); A-1 kayıtlı, A-2 yeni.
    data = xlsx(unit_row("a", "1", "yeni malik"), unit_row("a", "2"))

    preview = (await upload(api, world, data)).json()
    assert [r["already_exists"] for r in preview["rows"]] == [True, False]
    assert (preview["new_count"], preview["existing_count"]) == (1, 1)

    result = (await confirm(api, world, preview["import_id"])).json()

    assert result["data"]["skipped"] == ["a-1"]
    assert result["data"]["created_blocks"] == 0
    assert (
        result["message"]
        == "1 bölüm ve 1 kişi aktarıldı. 1 bölüm zaten kayıtlı olduğu için atlandı."
    )
    with site_scope(world.a):
        async with session_factory() as session:
            names: set[str] = set(await session.scalars(select(Person.last_name)))
            blocks: Sequence[str] = (await session.scalars(select(Block.name))).all()
    assert names == {"MALİK", "YILMAZ"}  # eski malik yerinde, "yeni malik" yazılmadı
    assert blocks == ["A"]


async def test_reference_code_collision_gets_a_number(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    await create_unit_with_owner(api, world, "A1", "2")  # kod tabanı A12 → A12-M, A12-O
    preview = (await upload(api, world, xlsx(unit_row("A", "12")))).json()
    await confirm(api, world, preview["import_id"])
    with site_scope(world.a):
        async with session_factory() as session:
            codes: set[str] = set(await session.scalars(select(LedgerAccount.reference_code)))
    assert codes == {"A12-M", "A12-O", "A12-M2", "A12-O2"}


async def test_nothing_new_means_no_import_id(api: httpx2.AsyncClient, world: World) -> None:
    body = (await upload(api, world, xlsx(unit_row("A", "", "ali can")))).json()
    assert (body["importable_count"], body["import_id"], body["expires_at"]) == (0, None, None)
    assert not world.store.root.exists() or list(world.store.root.glob("*/*/*")) == []


# --- tek sefer, yalnız yükleyen, yalnız o site ------------------------------------


async def test_confirm_works_once(api: httpx2.AsyncClient, world: World) -> None:
    import_id = (await upload(api, world, xlsx(unit_row("A", "1")))).json()["import_id"]
    assert (await confirm(api, world, import_id)).status_code == 200
    again = await confirm(api, world, import_id)
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "import_not_found"


async def test_import_is_bound_to_uploader_and_site(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    import_id = (await upload(api, world, xlsx(unit_row("A", "1")))).json()["import_id"]
    other = await create_user(session_factory, "diger@test.local")
    await add_site_membership(session_factory, world.a, other.id, "Yönetici")
    other_headers = await login_headers(api, "diger@test.local")

    assert (await confirm(api, world, import_id, headers=other_headers)).status_code == 404
    assert (await confirm(api, world, import_id, slug="yildiz-sitesi")).status_code == 404
    assert (await counts(session_factory, world.b))["units"] == 0
    assert (await confirm(api, world, import_id)).status_code == 200  # sahibi hâlâ onaylar


async def test_expired_import_cannot_be_confirmed(api: httpx2.AsyncClient, world: World) -> None:
    import_id = (await upload(api, world, xlsx(unit_row("A", "1")))).json()["import_id"]
    [path] = list(world.store.root.glob("*/*/*"))
    old = path.stat().st_mtime - 6 * 3600
    os.utime(path, (old, old))

    assert (await confirm(api, world, import_id)).status_code == 404
    assert list(world.store.root.glob("*/*/*")) == []


async def test_conflict_rolls_back_and_keeps_the_file(
    api: httpx2.AsyncClient,
    world: World,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = (await upload(api, world, xlsx(unit_row("A", "1")))).json()["import_id"]
    original = import_svc.apply_import

    async def racing(session: AsyncSession, *args: object, **kwargs: object) -> object:
        await original(session, *args, **kwargs)  # type: ignore[arg-type]
        raise IntegrityError("INSERT", {}, Exception("unique"))

    monkeypatch.setattr(import_svc, "apply_import", racing)
    conflict = await confirm(api, world, import_id)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "import_conflict"
    assert (await counts(session_factory, world.a))["units"] == 0  # hepsi ya da hiçbiri

    monkeypatch.setattr(import_svc, "apply_import", original)
    assert (await confirm(api, world, import_id)).status_code == 200


# --- yetki ve erişim ----------------------------------------------------------------


async def test_permission_is_units_manage(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    accountant = await create_user(session_factory, "muhasebe@test.local")
    await add_site_membership(session_factory, world.a, accountant.id, "Muhasebe")
    headers = await login_headers(api, "muhasebe@test.local")
    import_id = (await upload(api, world, xlsx(unit_row("A", "1")))).json()["import_id"]

    template = await api.get(url("aksu-konaklari", "/template.xlsx"), headers=headers)
    preview = await upload(api, world, xlsx(unit_row("A", "1")), headers=headers)
    confirmed = await confirm(api, world, import_id, headers=headers)
    no_site = await upload(
        api, world, xlsx(unit_row("A", "1")), slug="yildiz-sitesi", headers=headers
    )

    assert [template.status_code, preview.status_code, confirmed.status_code] == [403, 403, 403]
    assert no_site.status_code == 404  # siteye erişim yoksa site yok
    anonymous = await api.post(url("aksu-konaklari"), files={"file": ("a.xlsx", b"PK")})
    assert anonymous.status_code == 401


# --- dosya hataları -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("fatura.pdf", b"%PDF-1.4", "invalid_file_type"),
        ("sahte.xlsx", b"%PDF-1.4 <script>", "invalid_file_content"),
        ("bos.xlsx", None, "missing_columns"),
    ],
)
async def test_bad_files_are_rejected(
    api: httpx2.AsyncClient, world: World, name: str, content: bytes | None, code: str
) -> None:
    data = content if content is not None else xlsx(header=["Blok"])
    response = await upload(api, world, data, name=name)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == {"file": error["message"]}


async def test_file_over_5_mb_is_413(api: httpx2.AsyncClient, world: World) -> None:
    just_over = b"PK\x03\x04" + b"0" * import_svc.MAX_UPLOAD_BYTES
    response = await upload(api, world, just_over)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"

    far_over = b"0" * (import_svc.MAX_UPLOAD_BYTES * 2)  # ara katman ayrıştırmadan keser
    response = await upload(api, world, far_over)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


# --- plan tavanı (öneri, engellemez) --------------------------------------------------


async def test_plan_limit_warns_but_does_not_block(
    api: httpx2.AsyncClient, world: World, session_factory: Factory
) -> None:
    async with session_factory() as session, session.begin():
        plan = Plan(name="Küçük", max_units=1, allowed_modules=["finance"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.a)
        assert site is not None
        site.plan_id = plan.id

    body = (await upload(api, world, xlsx(unit_row("A", "1"), unit_row("A", "2")))).json()

    assert body["issues"][0]["row_number"] is None
    assert body["issues"][0]["severity"] == "warning"
    assert "sınırı 1" in body["issues"][0]["message"]
    assert body["warning_count"] == 1
    assert (await confirm(api, world, body["import_id"])).status_code == 200
