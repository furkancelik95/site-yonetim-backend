"""OpenAPI şema adları — frontend tipleri bu adlardan üretir (`npm run api:types`).

İki modülde aynı adlı model olunca FastAPI ikisini de `site_yonetim__api__v1__…` diye
uzun ada çevirir ve frontend'de `Schemas["RequestOut"]` gibi kullanımlar kırılır. Aşağıdaki
liste frontend'in bugün uzun adla kullandığı eski çakışmalardır; yeni model benzersiz ad alır.
"""

from site_yonetim.main import create_app

KNOWN_QUALIFIED = {
    "site_yonetim__api__v1__bank_imports__PreviewOut",
    "site_yonetim__api__v1__cash__StatementOut",
    "site_yonetim__api__v1__charges__PlanRef",
    "site_yonetim__api__v1__charges__PreviewOut",
    "site_yonetim__api__v1__charges__ReverseIn",
    "site_yonetim__api__v1__expenses__ReverseIn",
    "site_yonetim__api__v1__payments__AccountOut",
    "site_yonetim__api__v1__payments__StatementOut",
    "site_yonetim__api__v1__reports__PlanRef",
    "site_yonetim__api__v1__structure__AccountOut",
}


def test_yeni_sema_adi_cakismasi_yok() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    qualified = {name for name in schemas if name.startswith("site_yonetim__")}
    assert qualified == KNOWN_QUALIFIED
