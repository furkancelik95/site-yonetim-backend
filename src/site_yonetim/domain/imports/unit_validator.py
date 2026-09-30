"""Excel'den bölüm ve kişi aktarımı — satır doğrulayıcı, saf (docs/11 §2–§3, docs/07 §5).

**Hatalı satır koşuyu durdurmaz:** hata → satır aktarılmaz; uyarı → satır aktarılır, sorunlu
alan boş bırakılır ya da varsayılan kullanılır. Yalnız dosyanın kendisi okunamıyorsa
(zorunlu sütun yok, satır sınırı aşıldı) `ImportFileError` fırlatılır.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from site_yonetim.domain.imports.numbers import CellValue, cell_decimal, cell_text
from site_yonetim.domain.structure import UnitUsage
from site_yonetim.domain.text import ascii_fold, normalize_person_name, tr_lower
from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_name_part,
    normalize_tr_mobile,
)

MAX_DATA_ROWS = 5000
BLOCK_NAME_MAX = 40
UNIT_NUMBER_MAX = 20
UNIT_TYPE_MAX = 40
AREA_MIN, AREA_MAX = Decimal(1), Decimal(10_000)
FLOOR_MIN, FLOOR_MAX = -5, 100
_CENT = Decimal("0.01")

type _Normalizer = Callable[[str], str]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Column(StrEnum):
    """Değer: şablondaki başlık — sorunlarda sütun adı olarak da gösterilir."""

    BLOCK = "Blok"
    NUMBER = "Daire No"
    FLOOR = "Kat"
    UNIT_TYPE = "Daire Tipi"
    GROSS_AREA = "Brüt m²"
    NET_AREA = "Net m²"
    LAND_NUMERATOR = "Arsa Payı Pay"
    LAND_DENOMINATOR = "Arsa Payı Payda"
    USAGE = "Kullanım"
    OWNER_FIRST = "Malik Ad"
    OWNER_LAST = "Malik Soyad"
    OWNER_PHONE = "Malik Telefon"
    OWNER_EMAIL = "Malik E-posta"
    TENANT_FIRST = "Kiracı Ad"
    TENANT_LAST = "Kiracı Soyad"
    TENANT_PHONE = "Kiracı Telefon"
    TENANT_EMAIL = "Kiracı E-posta"


REQUIRED_COLUMNS = (Column.NUMBER, Column.OWNER_FIRST, Column.OWNER_LAST)


def _person_aliases(prefix: str) -> dict[str, tuple[str, ...]]:
    return {
        "first": (f"{prefix} ad", f"{prefix} adı"),
        "last": (f"{prefix} soyad", f"{prefix} soyadı"),
        "phone": (f"{prefix} telefon",),
        "email": (f"{prefix} e-posta", f"{prefix} eposta"),
    }


_OWNER, _TENANT = _person_aliases("malik"), _person_aliases("kiracı")

# docs/11 §2 — eş anlamlı başlıklar (Türkçe kurallarla küçük harf)
_ALIASES: dict[Column, tuple[str, ...]] = {
    Column.BLOCK: ("blok", "blok adı"),
    Column.NUMBER: ("daire no", "daire", "bağımsız bölüm no", "no"),
    Column.FLOOR: ("kat",),
    Column.UNIT_TYPE: ("daire tipi", "tip"),
    Column.GROSS_AREA: ("brüt m2", "brüt m²", "brüt"),
    Column.NET_AREA: ("net m2", "net m²", "net"),
    Column.LAND_NUMERATOR: ("arsa payı pay", "arsa payı", "pay"),
    Column.LAND_DENOMINATOR: ("arsa payı payda", "payda"),
    Column.USAGE: ("kullanım", "kullanım tipi"),
    Column.OWNER_FIRST: _OWNER["first"],
    Column.OWNER_LAST: _OWNER["last"],
    Column.OWNER_PHONE: _OWNER["phone"],
    Column.OWNER_EMAIL: _OWNER["email"],
    Column.TENANT_FIRST: _TENANT["first"],
    Column.TENANT_LAST: _TENANT["last"],
    Column.TENANT_PHONE: _TENANT["phone"],
    Column.TENANT_EMAIL: _TENANT["email"],
}

_USAGE_WORDS: dict[str, UnitUsage] = {
    **dict.fromkeys(("konut", "mesken", "daire"), UnitUsage.RESIDENTIAL),
    **dict.fromkeys(
        ("ticari", "dükkan", "dukkan", "ofis", "işyeri", "isyeri"), UnitUsage.COMMERCIAL
    ),
    "depo": UnitUsage.STORAGE,
    **dict.fromkeys(("otopark", "garaj"), UnitUsage.PARKING),
}


def match_key(text: str) -> str:
    """Başlık ve kullanım eşlemesi: kırpılmış, ASCII'ye katlanmış küçük harf."""
    return ascii_fold(cell_text(text))


_HEADER_LOOKUP: dict[str, Column] = {
    match_key(alias): column for column, aliases in _ALIASES.items() for alias in aliases
}
_USAGES: dict[str, UnitUsage] = {match_key(word): usage for word, usage in _USAGE_WORDS.items()}


class ImportFileError(Exception):
    """Dosya bütün olarak işlenemiyor — satır satır raporlanacak bir şey yok."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Issue:
    row_number: int | None  # Excel satır numarası; dosya geneli için None
    column: str | None
    message: str
    severity: Severity


@dataclass(frozen=True, slots=True)
class PersonData:
    first_name: str  # `Ayşe`
    last_name: str  # `YILMAZ`
    phone: str | None  # E.164
    email: str | None


@dataclass(frozen=True, slots=True)
class UnitRow:
    row_number: int
    block: str  # tek bloklu sitede boş olabilir
    number: str
    floor: int | None
    unit_type: str | None
    gross_area: Decimal | None
    net_area: Decimal | None
    land_share_numerator: int | None
    land_share_denominator: int | None
    usage: UnitUsage
    owner: PersonData
    tenant: PersonData | None

    @property
    def display_name(self) -> str:
        return display_name(self.block, self.number)

    @property
    def key(self) -> tuple[str, str]:
        return unit_key(self.block, self.number)


def display_name(block: str, number: str) -> str:
    return f"{block}-{number}" if block else number


def unit_key(block: str, number: str) -> tuple[str, str]:
    """Aynı bölüm mü? Blok adı Türkçe kurallarla harf duyarsız, numara olduğu gibi."""
    return tr_lower(" ".join(block.split())), number


@dataclass(frozen=True, slots=True)
class ValidationResult:
    rows: list[UnitRow]  # aktarılabilir satırlar
    issues: list[Issue]
    total_rows: int  # boş olmayan veri satırı sayısı

    @property
    def importable_count(self) -> int:
        return len(self.rows)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.WARNING)


def map_headers(
    header: Sequence[CellValue], row_number: int
) -> tuple[dict[Column, int], list[Issue]]:
    """Başlık satırı → sütun konumları. Sıra önemsiz; tanınmayan başlıklar yok sayılır."""
    positions: dict[Column, int] = {}
    issues: list[Issue] = []
    for index, raw in enumerate(header):
        column = _HEADER_LOOKUP.get(match_key(cell_text(raw)))
        if column is None:
            continue
        if column in positions:
            issues.append(
                Issue(
                    row_number,
                    column.value,
                    f"'{cell_text(raw)}' başlığı birden çok sütunda var; ilki kullanıldı.",
                    Severity.WARNING,
                )
            )
            continue
        positions[column] = index
    missing = [c.value for c in REQUIRED_COLUMNS if c not in positions]
    if missing:
        names = ", ".join(f"'{name}'" for name in missing)
        raise ImportFileError(
            "missing_columns",
            f"Dosyada {names} sütunu bulunamadı. Şablonu indirip başlıkları kontrol edin.",
        )
    return positions, issues


def validate_sheet(
    rows: Iterable[Sequence[CellValue]], *, max_rows: int = MAX_DATA_ROWS
) -> ValidationResult:
    """Sayfanın tüm satırları (ilk dolu satır başlık). Satır numaraları 1'den başlar."""
    positions: dict[Column, int] | None = None
    issues: list[Issue] = []
    valid: list[UnitRow] = []
    seen: dict[tuple[str, str], int] = {}
    total = 0
    for row_number, cells in enumerate(rows, start=1):
        if all(not cell_text(value) for value in cells):
            continue  # boş satır sessizce atlanır
        if positions is None:
            positions, header_issues = map_headers(cells, row_number)
            issues.extend(header_issues)
            continue
        total += 1
        if total > max_rows:
            raise ImportFileError(
                "too_many_rows",
                f"Bir dosyada en fazla {max_rows:,} satır aktarılabilir; dosyayı bölün.".replace(
                    ",", "."
                ),
            )
        reader = _RowReader(row_number, cells, positions)
        row = reader.read()
        if row is not None:
            first = seen.get(row.key)
            if first is not None:
                reader.error(Column.NUMBER, f"'{row.display_name}' zaten {first}. satırda var.")
                row = None
            else:
                seen[row.key] = row_number
        issues.extend(reader.issues)
        if row is not None:
            valid.append(row)
    if positions is None:
        raise ImportFileError("empty_file", "Dosyada başlık satırı ve veri bulunamadı.")
    return ValidationResult(valid, issues, total)


def _format_decimal_tr(value: Decimal) -> str:
    integer, _, fraction = f"{value:.2f}".partition(".")
    return f"{int(integer):,}".replace(",", ".") + f",{fraction}"


class _RowReader:
    def __init__(
        self, row_number: int, cells: Sequence[CellValue], positions: dict[Column, int]
    ) -> None:
        self.row_number = row_number
        self.cells = cells
        self.positions = positions
        self.issues: list[Issue] = []
        self.failed = False

    # --- yardımcılar ---

    def raw(self, column: Column) -> CellValue:
        index = self.positions.get(column)
        if index is None or index >= len(self.cells):
            return None
        return self.cells[index]

    def text(self, column: Column) -> str:
        return cell_text(self.raw(column))

    def error(self, column: Column, message: str) -> None:
        self.failed = True
        self.issues.append(Issue(self.row_number, column.value, message, Severity.ERROR))

    def warning(self, column: Column, message: str) -> None:
        self.issues.append(Issue(self.row_number, column.value, message, Severity.WARNING))

    def number(self, column: Column) -> Decimal | None:
        try:
            return cell_decimal(self.raw(column))
        except ValueError:
            self.error(column, f"'{self.text(column)}' bir sayı değil.")
            return None

    # --- alanlar ---

    def read(self) -> UnitRow | None:
        block = self.text(Column.BLOCK)
        if len(block) > BLOCK_NAME_MAX:
            self.error(Column.BLOCK, f"Blok adı en fazla {BLOCK_NAME_MAX} karakter olabilir.")
        number = self.text(Column.NUMBER)
        if not number:
            self.error(Column.NUMBER, "Daire No boş olamaz.")
        elif len(number) > UNIT_NUMBER_MAX:
            self.error(Column.NUMBER, f"Daire No en fazla {UNIT_NUMBER_MAX} karakter olabilir.")
        floor = self.floor()
        unit_type = self.unit_type()
        gross, net = self.area(Column.GROSS_AREA), self.area(Column.NET_AREA)
        if gross is not None and net is not None and net > gross:
            self.warning(
                Column.NET_AREA,
                f"Net alan ({_format_decimal_tr(net)}) brütten "
                f"({_format_decimal_tr(gross)}) büyük görünüyor.",
            )
        numerator, denominator = self.land_share()
        usage = self.usage()
        owner = self.person(
            "Malik",
            (Column.OWNER_FIRST, Column.OWNER_LAST, Column.OWNER_PHONE, Column.OWNER_EMAIL),
            required=True,
        )
        tenant = self.person(
            "Kiracı",
            (Column.TENANT_FIRST, Column.TENANT_LAST, Column.TENANT_PHONE, Column.TENANT_EMAIL),
            required=False,
        )
        if self.failed or owner is None:
            return None
        return UnitRow(
            row_number=self.row_number,
            block=block,
            number=number,
            floor=floor,
            unit_type=unit_type,
            gross_area=gross,
            net_area=net,
            land_share_numerator=numerator,
            land_share_denominator=denominator,
            usage=usage,
            owner=owner,
            tenant=tenant,
        )

    def floor(self) -> int | None:
        text = self.text(Column.FLOOR)
        if not text:
            return None
        try:
            value = cell_decimal(self.raw(Column.FLOOR))
        except ValueError:
            value = None
        if (
            value is None
            or value != value.to_integral_value()
            or not (FLOOR_MIN <= value <= FLOOR_MAX)
        ):
            self.warning(
                Column.FLOOR,
                f"'{text}' geçerli bir kat değil ({FLOOR_MIN} ile {FLOOR_MAX} arasında tam sayı "
                "olmalı); kat boş bırakıldı.",
            )
            return None
        return int(value)

    def unit_type(self) -> str | None:
        text = self.text(Column.UNIT_TYPE)
        if len(text) > UNIT_TYPE_MAX:
            self.warning(
                Column.UNIT_TYPE,
                f"Daire tipi en fazla {UNIT_TYPE_MAX} karakter olabilir; tip boş bırakıldı.",
            )
            return None
        return text or None

    def area(self, column: Column) -> Decimal | None:
        value = self.number(column)
        if value is None:
            return None
        value = value.quantize(_CENT, rounding=ROUND_HALF_UP)
        if not AREA_MIN <= value <= AREA_MAX:
            self.warning(
                column,
                f"'{self.text(column)}' 1 ile 10.000 m² arasında olmalı; alan boş bırakıldı.",
            )
            return None
        return value

    def share_part(self, column: Column) -> int | None:
        value = self.number(column)
        if value is None:
            return None
        if value != value.to_integral_value() or value < 1:
            self.error(column, f"'{self.text(column)}' 1 veya daha büyük bir tam sayı olmalı.")
            return None
        return int(value)

    def land_share(self) -> tuple[int | None, int | None]:
        has_numerator = bool(self.text(Column.LAND_NUMERATOR))
        has_denominator = bool(self.text(Column.LAND_DENOMINATOR))
        if has_numerator != has_denominator:
            missing = Column.LAND_DENOMINATOR if has_numerator else Column.LAND_NUMERATOR
            self.error(missing, "Arsa payı için pay ve payda birlikte girilmeli.")
            return None, None
        numerator = self.share_part(Column.LAND_NUMERATOR)
        denominator = self.share_part(Column.LAND_DENOMINATOR)
        if numerator is None or denominator is None:
            return None, None
        if numerator > denominator:
            self.warning(
                Column.LAND_NUMERATOR,
                f"Arsa payı ({numerator}) paydadan ({denominator}) büyük görünüyor; "
                "arsa payı boş bırakıldı.",
            )
            return None, None
        return numerator, denominator

    def usage(self) -> UnitUsage:
        text = self.text(Column.USAGE)
        if not text:
            return UnitUsage.RESIDENTIAL
        usage = _USAGES.get(match_key(text))
        if usage is None:
            self.warning(Column.USAGE, f"'{text}' tanınmadı; konut olarak alındı.")
            return UnitUsage.RESIDENTIAL
        return usage

    def person(
        self, label: str, columns: tuple[Column, Column, Column, Column], *, required: bool
    ) -> PersonData | None:
        first_col, last_col, phone_col, email_col = columns
        first, last = self.text(first_col), self.text(last_col)
        phone, email = self.text(phone_col), self.text(email_col)
        if not (first or last or phone or email):
            if required:
                self.error(
                    first_col,
                    f"Her bağımsız bölümün maliki olmalı; {first_col.value} ve "
                    f"{last_col.value} girin.",
                )
            return None
        names_ok = True
        for column, value in ((first_col, first), (last_col, last)):
            if not value:
                self.error(column, f"{column.value} boş olamaz.")
                names_ok = False
                continue
            try:
                normalize_name_part(value, column.value)
            except ValueError as exc:
                self.error(column, f"'{value}': {exc}")
                names_ok = False
        if not names_ok:
            return None
        first, last = normalize_person_name(first, last)
        return PersonData(
            first_name=first,
            last_name=last,
            phone=self.optional(phone_col, phone, normalize_tr_mobile, "geçerli bir cep telefonu"),
            email=self.optional(email_col, email, normalize_email_address, "geçerli bir e-posta"),
        )

    def optional(self, column: Column, value: str, normalize: _Normalizer, what: str) -> str | None:
        if not value:
            return None
        try:
            return normalize(value)
        except ValueError:
            self.warning(column, f"'{value}' {what} değil; boş bırakıldı.")
            return None
