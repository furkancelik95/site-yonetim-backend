"""Belge arşivi saf kuralları — Yazılım Dokümanı RAP-03, 18.2; servis isteği 78."""

from datetime import date

import pytest

from site_yonetim.domain.documents import (
    Confidentiality,
    DocumentFieldError,
    DocumentInput,
    DocumentKind,
    DocumentScope,
    ExpiryWarning,
    check_document,
    check_file,
    days_left,
    expiry_message,
    expiry_warning,
    title_key,
)
from site_yonetim.domain.files import MAX_FILE_BYTES, kind_from_content, name_with_extension

PDF = b"%PDF-1.7\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def _input(**overrides: object) -> DocumentInput:
    values: dict[str, object] = {
        "title": "Yönetim planı",
        "kind": "plan",
        "scope": "site",
        "has_scope_target": False,
        "confidentiality": "public",
        "retention_years": None,
    }
    values.update(overrides)
    return DocumentInput(**values)  # type: ignore[arg-type]


def test_ad_anahtari_turkce_harf_ve_bosluk_duyarsiz() -> None:
    assert title_key("  YÖNETİM   PLANI ") == title_key("yönetim planı") == "yönetim planı"
    assert title_key("IŞIK") == "ışık"


def test_gecerli_belge() -> None:
    checked = check_document(_input(title="  Site   kuralları ", retention_years=10))
    assert checked.title == "Site kuralları"
    assert checked.kind is DocumentKind.PLAN
    assert checked.scope is DocumentScope.SITE
    assert checked.confidentiality is Confidentiality.PUBLIC
    assert checked.retention_years == 10


def test_alan_hatalari_birlikte() -> None:
    with pytest.raises(DocumentFieldError) as exc:
        check_document(
            _input(title="ab", kind="x", scope="block", confidentiality="?", retention_years=101)
        )
    assert exc.value.fields == {
        "title": "Belge adını yazın.",
        "kind": "Tür seçin.",
        "scope": "Blok ya da bölüm seçin.",
        "confidentiality": "Gizlilik seçin.",
        "retention_years": "Saklama süresi 1–100 yıl arasında olmalı.",
    }


def test_bolume_ozel_belge_bolum_kapsami_ister() -> None:
    with pytest.raises(DocumentFieldError) as exc:
        check_document(_input(scope="block", has_scope_target=True, confidentiality="unit"))
    assert exc.value.fields == {"confidentiality": "Bölüme özel belge bir bölüme bağlı olmalı."}
    ok = check_document(_input(scope="unit", has_scope_target=True, confidentiality="unit"))
    assert ok.confidentiality is Confidentiality.UNIT


def test_cok_uzun_ad() -> None:
    with pytest.raises(DocumentFieldError) as exc:
        check_document(_input(title="a" * 121))
    assert exc.value.fields == {"title": "Belge adı en fazla 120 karakter olabilir."}


@pytest.mark.parametrize(
    ("data", "content_type"),
    [(PDF, "application/pdf"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_tur_icerikten(data: bytes, content_type: str) -> None:
    kind = kind_from_content(data)
    assert kind is not None
    assert kind.content_type == content_type


def test_dosya_kontrolu() -> None:
    assert check_file("rapor.PDF", PDF).file_name == "rapor.PDF"
    assert check_file("foto.pdf", JPEG).file_name == "foto.jpg"
    assert check_file("adsiz", PDF).file_name == "adsiz.pdf"
    assert check_file(None, PDF).file_name == "belge.pdf"
    for data, message in (
        (b"", "Dosya seçin."),
        (b"GIF89a", "Yalnız PDF, PNG, JPEG ya da WebP (dosya içeriği doğrulandı)."),
        (PDF + b"0" * MAX_FILE_BYTES, "Dosya en fazla 10 MB."),
    ):
        with pytest.raises(DocumentFieldError) as exc:
            check_file("x.pdf", data)
        assert exc.value.fields == {"file": message}


def test_uzanti_duzeltme_uzun_adda_sinir() -> None:
    kind = kind_from_content(PDF)
    assert kind is not None
    fixed = name_with_extension("a" * 120, kind)
    assert len(fixed) == 120
    assert fixed.endswith(".pdf")
    assert name_with_extension("not.v2", kind) == "not.v2.pdf"


@pytest.mark.parametrize(
    ("until", "left", "warning"),
    [
        (None, None, None),
        (date(2026, 8, 20), 61, None),
        (date(2026, 8, 19), 60, ExpiryWarning.DAYS_60),
        (date(2026, 7, 21), 31, ExpiryWarning.DAYS_60),
        (date(2026, 7, 20), 30, ExpiryWarning.DAYS_30),
        (date(2026, 6, 20), 0, ExpiryWarning.DAYS_30),
        (date(2026, 6, 19), -1, ExpiryWarning.EXPIRED),
    ],
)
def test_gecerlilik_esikleri(
    until: date | None, left: int | None, warning: ExpiryWarning | None
) -> None:
    assert days_left(until, date(2026, 6, 20)) == left
    assert expiry_warning(left) is warning


def test_uyari_metni() -> None:
    assert expiry_message("Poliçe", ExpiryWarning.DAYS_30, 12) == (
        "Poliçe: geçerliliğin bitmesine 12 gün kaldı; yenilemeyi planlayın."
    )
    assert expiry_message("Poliçe", ExpiryWarning.EXPIRED, -3) == (
        "Poliçe: geçerlilik süresi doldu; yenisini yükleyin."
    )
