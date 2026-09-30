"""Excel içe aktarma doğrulayıcısı — docs/07 §5 altın testleri ve sayı okuma (docs/11 §3.1)."""

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from site_yonetim.domain.imports.numbers import cell_decimal, cell_text, parse_decimal
from site_yonetim.domain.imports.unit_validator import (
    Column,
    ImportFileError,
    Issue,
    Severity,
    ValidationResult,
    map_headers,
    validate_sheet,
)
from site_yonetim.domain.structure import UnitUsage

HEADER = [c.value for c in Column]


def row(**values: Any) -> list[Any]:
    """Şablon sırasında bir satır; anahtarlar Column adları (küçük harf)."""
    defaults: dict[str, Any] = {
        "block": "A",
        "number": "1",
        "owner_first": "Ayşe",
        "owner_last": "Yılmaz",
    }
    defaults.update(values)
    return [defaults.get(c.name.lower()) for c in Column]


def check(*rows: list[Any]) -> ValidationResult:
    return validate_sheet([HEADER, *rows])


def issues_for(result: ValidationResult, column: Column) -> list[Issue]:
    return [i for i in result.issues if i.column == column.value]


def only_issue(result: ValidationResult, column: Column, severity: Severity) -> Issue:
    found = issues_for(result, column)
    assert len(found) == 1, result.issues
    assert found[0].severity is severity
    return found[0]


# --- §3.1 sayı okuma (07 §5.3) --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("104,50", "104.50"),
        ("104.50", "104.50"),
        ("104.5", "104.5"),
        ("68", "68"),
        ("1.234", "1234"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 234,5", "1234.5"),
        ("1 234,5", "1234.5"),  # bölünmez boşluk
        ("1.234.567", "1234567"),
        ("1.2345", "1.2345"),
        ("0.250", "0.250"),  # tam kısım 0 → binlik olamaz
        ("-3", "-3"),
        ("3,0", "3.0"),
    ],
)
def test_parse_decimal_reads_separator_position(text: str, expected: str) -> None:
    assert parse_decimal(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["abc", "", "1,2,3", "1.23.4", "12a", "1..2", ".", "1e5", "NaN"])
def test_parse_decimal_rejects_non_numbers(text: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(text)}$"):
        parse_decimal(text)


def test_cell_values_from_excel_types() -> None:
    assert cell_text(None) == ""
    assert cell_text(5.0) == "5"
    assert cell_text(104.5) == "104.5"
    assert cell_text(5321234567) == "5321234567"
    assert cell_text("  A   Blok \n") == "A Blok"
    assert cell_text(date(2026, 1, 2)) == "2026-01-02"
    assert cell_decimal(104.5) == Decimal("104.5")
    assert cell_decimal(68) == Decimal(68)
    assert cell_decimal(Decimal("1.5")) == Decimal("1.5")
    assert cell_decimal("  ") is None
    for bad in (True, float("nan"), float("inf"), Decimal("NaN"), date(2026, 1, 1)):
        with pytest.raises(ValueError, match=re.escape(cell_text(bad) or str(bad))):
            cell_decimal(bad)


# --- 07 §5 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone",
    [
        "5321234567",
        "05321234567",
        "905321234567",
        "+90 532 123 45 67",
        "0532 123 45 67",
        "532-123-45-67",
        5321234567,
    ],
)
def test_5_1_phone_is_normalized_to_e164(phone: object) -> None:
    result = check(row(owner_phone=phone))
    assert result.issues == []
    assert result.rows[0].owner.phone == "+905321234567"


@pytest.mark.parametrize("phone", ["2121234567", "53212345", "abc"])
def test_5_2_invalid_phone_warns_but_row_is_imported(phone: str) -> None:
    result = check(row(owner_phone=phone))
    issue = only_issue(result, Column.OWNER_PHONE, Severity.WARNING)
    assert f"'{phone}'" in issue.message
    assert result.importable_count == 1
    assert result.rows[0].owner.phone is None


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        ("104,50", "104.50"),
        ("104.50", "104.50"),
        ("104.5", "104.5"),
        ("68", "68"),
        ("1.234", "1234"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 234,5", "1234.5"),
        (104.5, "104.5"),
        ("86,456", "86.46"),  # 2 ondalığa yuvarlanır (ROUND_HALF_UP)
    ],
)
def test_5_3_area_is_culture_independent(gross: object, expected: str) -> None:
    result = check(row(gross_area=gross))
    assert result.issues == []
    assert result.rows[0].gross_area == Decimal(expected)


def test_5_4_non_numeric_area_is_error() -> None:
    result = check(row(gross_area="abc"))
    issue = only_issue(result, Column.GROSS_AREA, Severity.ERROR)
    assert issue.message == "'abc' bir sayı değil."
    assert issue.row_number == 2
    assert result.importable_count == 0


@pytest.mark.parametrize("area", ["0,5", "10001", "-3"])
def test_area_out_of_range_warns_and_is_left_empty(area: str) -> None:
    result = check(row(net_area=area))
    only_issue(result, Column.NET_AREA, Severity.WARNING)
    assert result.rows[0].net_area is None


def test_5_5_net_greater_than_gross_warns() -> None:
    result = check(row(gross_area="80", net_area="90"))
    issue = only_issue(result, Column.NET_AREA, Severity.WARNING)
    assert issue.message == "Net alan (90,00) brütten (80,00) büyük görünüyor."
    assert result.importable_count == 1
    assert result.rows[0].net_area == Decimal("90.00")


@pytest.mark.parametrize(
    ("values", "missing"),
    [
        ({"land_numerator": "45"}, Column.LAND_DENOMINATOR),
        ({"land_denominator": "1000"}, Column.LAND_NUMERATOR),
    ],
)
def test_5_6_land_share_needs_both_parts(values: dict[str, str], missing: Column) -> None:
    result = check(row(**values))
    only_issue(result, missing, Severity.ERROR)
    assert result.importable_count == 0


def test_5_7_land_share_is_read() -> None:
    result = check(row(land_numerator="45", land_denominator="1000"))
    assert result.issues == []
    unit = result.rows[0]
    assert (unit.land_share_numerator, unit.land_share_denominator) == (45, 1000)


def test_land_share_numerator_above_denominator_warns_and_clears() -> None:
    result = check(row(land_numerator="60", land_denominator="50"))
    only_issue(result, Column.LAND_NUMERATOR, Severity.WARNING)
    assert result.rows[0].land_share_numerator is None
    assert result.rows[0].land_share_denominator is None


@pytest.mark.parametrize("value", ["0", "1,5", "abc"])
def test_land_share_parts_must_be_positive_integers(value: str) -> None:
    result = check(row(land_numerator=value, land_denominator="1000"))
    only_issue(result, Column.LAND_NUMERATOR, Severity.ERROR)
    assert result.importable_count == 0


def test_5_8_owner_is_required() -> None:
    result = check(row(owner_first=None, owner_last=None))
    issue = only_issue(result, Column.OWNER_FIRST, Severity.ERROR)
    assert issue.message.startswith("Her bağımsız bölümün maliki olmalı")
    assert result.importable_count == 0


@pytest.mark.parametrize(
    ("values", "column"),
    [({"owner_first": None}, Column.OWNER_FIRST), ({"owner_last": None}, Column.OWNER_LAST)],
)
def test_owner_needs_both_names(values: dict[str, None], column: Column) -> None:
    result = check(row(**values))
    only_issue(result, column, Severity.ERROR)
    assert result.importable_count == 0


def test_5_9_digit_in_name_is_error() -> None:
    result = check(row(owner_first="Ayşe2"))
    issue = only_issue(result, Column.OWNER_FIRST, Severity.ERROR)
    assert "'Ayşe2'" in issue.message
    assert "rakam" in issue.message


@pytest.mark.parametrize("name", ["A", "x" * 41])
def test_name_length_is_checked(name: str) -> None:
    result = check(row(owner_last=name))
    only_issue(result, Column.OWNER_LAST, Severity.ERROR)


def test_5_10_name_format_is_fixed() -> None:
    result = check(row(owner_first="aYşE", owner_last="yılmaz"))
    owner = result.rows[0].owner
    assert (owner.first_name, owner.last_name) == ("Ayşe", "YILMAZ")


def test_5_11_tenant_is_optional() -> None:
    result = check(row())
    assert result.issues == []
    assert result.rows[0].tenant is None


def test_tenant_follows_owner_rules() -> None:
    result = check(row(tenant_first="elif", tenant_last="demir", tenant_email=" Elif@Ornek.COM "))
    tenant = result.rows[0].tenant
    assert tenant is not None
    assert (tenant.first_name, tenant.last_name, tenant.email) == (
        "Elif",
        "DEMİR",
        "elif@ornek.com",
    )


def test_tenant_with_only_phone_needs_names() -> None:
    result = check(row(tenant_phone="5445556677"))
    assert {i.column for i in result.issues} == {"Kiracı Ad", "Kiracı Soyad"}
    assert result.importable_count == 0


def test_invalid_email_warns_and_is_left_empty() -> None:
    result = check(row(owner_email="ayse@ornek"))
    only_issue(result, Column.OWNER_EMAIL, Severity.WARNING)
    assert result.rows[0].owner.email is None


def test_5_12_same_number_in_same_block_twice_is_error() -> None:
    result = check(row(number="5"), row(number="5"))
    issue = only_issue(result, Column.NUMBER, Severity.ERROR)
    assert issue.row_number == 3
    assert issue.message == "'A-5' zaten 2. satırda var."
    assert result.importable_count == 1


def test_duplicate_block_name_is_case_insensitive_turkish() -> None:
    result = check(row(block="Irmak", number="5"), row(block="IRMAK", number="5"))
    assert result.importable_count == 1


def test_duplicate_of_an_invalid_row_does_not_block_the_valid_one() -> None:
    result = check(row(number="5", owner_first=None, owner_last=None), row(number="5"))
    assert result.importable_count == 1
    assert result.rows[0].row_number == 3


def test_5_13_same_number_in_different_blocks_is_fine() -> None:
    result = check(row(block="A", number="5"), row(block="B", number="5"))
    assert result.issues == []
    assert result.importable_count == 2


def test_5_14_empty_unit_number_is_error() -> None:
    result = check(row(number=None))
    only_issue(result, Column.NUMBER, Severity.ERROR)
    assert result.importable_count == 0


@pytest.mark.parametrize(
    ("text", "usage"),
    [
        ("Konut", UnitUsage.RESIDENTIAL),
        ("mesken", UnitUsage.RESIDENTIAL),
        ("ticari", UnitUsage.COMMERCIAL),
        ("Dükkan", UnitUsage.COMMERCIAL),
        ("İşyeri", UnitUsage.COMMERCIAL),
        ("ISYERI", UnitUsage.COMMERCIAL),
        ("DEPO", UnitUsage.STORAGE),
        ("otopark", UnitUsage.PARKING),
        ("Garaj", UnitUsage.PARKING),
        (None, UnitUsage.RESIDENTIAL),
    ],
)
def test_5_15_usage_is_read_flexibly(text: str | None, usage: UnitUsage) -> None:
    result = check(row(usage=text))
    assert result.issues == []
    assert result.rows[0].usage is usage


def test_5_16_unknown_usage_warns_and_defaults_to_residential() -> None:
    result = check(row(usage="villa"))
    only_issue(result, Column.USAGE, Severity.WARNING)
    assert result.rows[0].usage is UnitUsage.RESIDENTIAL


def test_5_17_blank_rows_are_skipped() -> None:
    result = validate_sheet([[None] * 3, HEADER, row(number="1"), [None, "  "], row(number="2")])
    assert result.issues == []
    assert result.total_rows == 2
    assert [r.row_number for r in result.rows] == [3, 5]  # Excel satır numaraları


def test_5_18_bad_row_does_not_block_others() -> None:
    result = check(row(number="1"), row(number="2", gross_area="abc"), row(number="3"))
    assert result.importable_count == 2
    assert result.error_count == 1
    assert result.total_rows == 3


# --- kat, tip, blok, numara ---------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("3,0", 3), (3.0, 3), ("-5", -5), ("100", 100)])
def test_floor_accepts_excel_leftovers(value: object, expected: int) -> None:
    result = check(row(floor=value))
    assert result.issues == []
    assert result.rows[0].floor == expected


@pytest.mark.parametrize("value", ["101", "-6", "2,5", "zemin"])
def test_invalid_floor_warns_and_is_left_empty(value: str) -> None:
    result = check(row(floor=value))
    only_issue(result, Column.FLOOR, Severity.WARNING)
    assert result.rows[0].floor is None


def test_long_texts() -> None:
    result = check(row(block="B" * 41), row(number="9" * 21), row(number="3", unit_type="T" * 41))
    assert [i.severity for i in result.issues] == [Severity.ERROR, Severity.ERROR, Severity.WARNING]
    assert result.importable_count == 1
    assert result.rows[0].unit_type is None


def test_single_block_site_may_leave_block_empty() -> None:
    result = check(row(block=None, number="7"))
    assert result.rows[0].block == ""
    assert result.rows[0].display_name == "7"


# --- başlıklar -------------------------------------------------------------------


def test_column_order_and_aliases_do_not_matter() -> None:
    header = ["  MALİK SOYADI ", "no", "Brüt M2", "malik adı", "Blok Adı", "bilinmeyen sütun"]
    result = validate_sheet([header, ["kaya", "12", "104,5", "mehmet", "C", "x"]])
    assert result.issues == []
    unit = result.rows[0]
    assert (unit.block, unit.number, unit.gross_area) == ("C", "12", Decimal("104.5"))
    assert (unit.owner.first_name, unit.owner.last_name) == ("Mehmet", "KAYA")


def test_turkish_dotted_capital_in_header() -> None:
    positions, _ = map_headers(["KİRACI AD", "Daire No", "MALİK AD", "Malik Soyad"], 1)
    assert positions[Column.TENANT_FIRST] == 0


def test_duplicate_header_warns_and_first_wins() -> None:
    result = validate_sheet(
        [["Daire No", "no", "Malik Ad", "Malik Soyad"], ["1", "2", "Ali", "Can"]]
    )
    only_issue(result, Column.NUMBER, Severity.WARNING)
    assert result.rows[0].number == "1"


def test_missing_required_columns_rejects_file() -> None:
    with pytest.raises(ImportFileError) as caught:
        validate_sheet([["Blok", "Malik Ad"], ["A", "Ali"]])
    assert caught.value.code == "missing_columns"
    assert "'Daire No'" in caught.value.message
    assert "'Malik Soyad'" in caught.value.message


def test_empty_sheet_is_rejected() -> None:
    with pytest.raises(ImportFileError) as caught:
        validate_sheet([[None, None], []])
    assert caught.value.code == "empty_file"


def test_row_limit() -> None:
    with pytest.raises(ImportFileError) as caught:
        validate_sheet([HEADER, row(number="1"), row(number="2"), row(number="3")], max_rows=2)
    assert caught.value.code == "too_many_rows"


def test_short_rows_are_padded() -> None:
    result = validate_sheet([HEADER, ["A", "1"]])
    only_issue(result, Column.OWNER_FIRST, Severity.ERROR)


def test_ascii_uppercase_headers_match() -> None:
    positions, _ = map_headers(["KIRACI AD", "DAIRE NO", "MALIK AD", "MALIK SOYADI", "KULLANIM"], 1)
    assert positions[Column.TENANT_FIRST] == 0
    assert positions[Column.USAGE] == 4
