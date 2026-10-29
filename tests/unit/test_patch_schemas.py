"""Kısmi düzenleme (PATCH) gövdeleri OpenAPI'de ekleme gövdesinden ayrı görünür.

Ekleme gövdesindeki varsayılan (ör. sayaçta `status: "active"`) PATCH'te "gönderilmezse
sıfırlanır" diye okunmasın: PATCH yalnız gönderilen alanları değiştirir (`model_fields_set`).
"""

from typing import Any

from site_yonetim.main import create_app


def _body_schema(spec: dict[str, Any], path: str, method: str) -> str:
    body = spec["paths"][path][method]["requestBody"]["content"]["application/json"]
    ref: str = body["schema"]["$ref"]
    return ref.rsplit("/", 1)[-1]


def test_patch_govdeleri_ayri_sema() -> None:
    spec = create_app().openapi()
    sites = "/api/v1/sites/{slug}"
    assert _body_schema(spec, f"{sites}/insurance-policies", "post") == "PolicyIn"
    assert _body_schema(spec, f"{sites}/insurance-policies/{{policy_id}}", "patch") == (
        "PolicyPatch"
    )
    assert _body_schema(spec, f"{sites}/meters", "post") == "MeterIn"
    assert _body_schema(spec, f"{sites}/meters/{{meter_id}}", "patch") == "MeterPatch"

    schemas = spec["components"]["schemas"]
    assert schemas["MeterIn"]["properties"]["status"]["default"] == "active"
    assert schemas["MeterPatch"]["properties"]["status"].get("default") is None
    for name in ("PolicyPatch", "MeterPatch"):
        assert "Kısmi düzenleme" in schemas[name]["description"]
        assert not schemas[name].get("required")
