"""Park yerleri ve tarihçeli tahsis — PM kararı K30, Yazılım Dokümanı OTP-01 (gerçek PostgreSQL).

Kabul (OTP-01 S1): B-14 A-1'e tahsisliyken aynı tarihte A-2'ye tahsis edilemez."""

from tests.integration.gatehouse_world import GateWorld


async def test_park_yeri_tanimi_ve_tahsis_cakismasi(gw: GateWorld) -> None:
    created = await gw.post(
        "/parking-spaces",
        gw.manager,
        {"code": "b-14", "kind": "standard", "location": "B blok -1", "is_paid": True},
    )
    assert created.status_code == 201, created.text
    space = created.json()["data"]
    assert (space["code"], space["kind"], space["is_paid"], space["current"]) == (
        "B-14",
        "standard",
        True,
        None,
    )
    dup = await gw.post("/parking-spaces", gw.manager, {"code": "B-14", "kind": "x"})
    assert dup.json()["error"]["fields"] == {
        "code": "Bu kodla park yeri var.",
        "kind": "Park yeri tipini seçin.",
    }
    await gw.post("/parking-spaces", gw.manager, {"code": "E-1", "kind": "ev"})

    allocated = await gw.post(
        f"/parking-spaces/{space['id']}/allocations",
        gw.manager,
        {"unit_id": gw.w.unit_id, "start_date": "2026-06-01"},
    )
    assert allocated.status_code == 201, allocated.text
    assert allocated.json()["message"] == "B-14, A-1 bölümüne tahsis edildi."
    allocation = allocated.json()["data"]
    assert allocation["is_current"] is True

    clash = await gw.post(
        f"/parking-spaces/{space['id']}/allocations",
        gw.manager,
        {"unit_id": gw.unit2, "start_date": "2026-06-20"},
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "space_taken"
    assert clash.json()["error"]["message"].startswith(
        "B-14 bu tarihte başka bölüme tahsisli (A-1)"
    )
    no_start = await gw.post(
        f"/parking-spaces/{space['id']}/allocations", gw.manager, {"unit_id": gw.unit2}
    )
    assert no_start.json()["error"]["fields"] == {"start_date": "Başlangıç tarihini seçin."}

    early = await gw.post(
        f"/parking-allocations/{allocation['id']}/end", gw.manager, {"end_date": "2026-05-01"}
    )
    assert early.status_code == 422
    ended = await gw.post(
        f"/parking-allocations/{allocation['id']}/end", gw.manager, {"end_date": "2026-06-30"}
    )
    assert ended.json()["data"]["end_date"] == "2026-06-30"
    twice = await gw.post(
        f"/parking-allocations/{allocation['id']}/end", gw.manager, {"end_date": "2026-07-01"}
    )
    assert twice.json()["error"]["code"] == "allocation_ended"
    after = await gw.post(
        f"/parking-spaces/{space['id']}/allocations",
        gw.manager,
        {"unit_id": gw.unit2, "start_date": "2026-07-01"},
    )
    assert after.status_code == 201, after.text

    listed = (await gw.get("/parking-spaces", gw.guard)).json()
    assert [(s["code"], (s["current"] or {}).get("unit_name")) for s in listed["items"]] == [
        ("B-14", "A-1"),
        ("E-1", None),
    ]
    history = (
        await gw.get("/parking-allocations", gw.manager, params={"space_id": space["id"]})
    ).json()
    assert [(a["unit_name"], a["start_date"]) for a in history["items"]] == [
        ("A-2", "2026-07-01"),
        ("A-1", "2026-06-01"),
    ]
    mine = (await gw.get("/resident/parking-spaces", gw.resident)).json()
    assert [s["code"] for s in mine] == ["B-14"]
    assert (await gw.get("/resident/parking-spaces", gw.neighbour)).json() == []
    filtered = (
        await gw.get("/parking-spaces", gw.manager, params={"unit_id": gw.w.unit_id})
    ).json()
    assert filtered["total"] == 1

    # yetki ve izolasyon
    assert (
        await gw.post("/parking-spaces", gw.guard, {"code": "X", "kind": "guest"})
    ).status_code == 403
    assert (await gw.get("/parking-spaces", gw.resident)).status_code == 403
    assert (await gw.get("/resident/parking-spaces", gw.guard)).status_code == 403
    other = await gw.api.get(gw.url("/parking-spaces", "yildiz-sitesi"), headers=gw.manager)
    assert other.json()["total"] == 0
    cross = await gw.api.post(
        gw.url(f"/parking-spaces/{space['id']}/allocations", "yildiz-sitesi"),
        headers=gw.manager,
        json={"unit_id": gw.w.unit_id, "start_date": "2026-08-01"},
    )
    assert cross.status_code == 404
