"""Mimari kurallar (docs/07-test-senaryolari.md §7).

7.4: `domain/` paketi `sqlalchemy`, `fastapi`, `starlette` içe aktarmaz ve saati okumaz.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "site_yonetim"
DOMAIN = SRC / "domain"

FORBIDDEN_IMPORTS = (
    "sqlalchemy",
    "fastapi",
    "starlette",
    "pydantic_settings",
    "site_yonetim.api",
    "site_yonetim.core",
    "site_yonetim.models",
    "site_yonetim.repositories",
    "site_yonetim.services",
)
FORBIDDEN_CALLS = {"now", "utcnow", "today"}


def _domain_files() -> list[Path]:
    return sorted(DOMAIN.rglob("*.py"))


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            found.extend(
                f"{path.name}:{node.lineno} import {module}"
                for module in modules
                if module.startswith(FORBIDDEN_IMPORTS)
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in FORBIDDEN_CALLS
        ):
            found.append(f"{path.name}:{node.lineno} .{node.func.attr}() çağrısı")
    return found


def test_domain_paketi_var() -> None:
    assert _domain_files(), "domain/ paketi bulunamadı"


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_saf_python(path: Path) -> None:
    assert _violations(path) == []


def test_kural_ihlali_yakalanir(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import sqlalchemy\n"
        "from fastapi import APIRouter\n"
        "from datetime import datetime\n"
        "x = datetime.now()\n",
        encoding="utf-8",
    )

    assert len(_violations(bad)) == 3
