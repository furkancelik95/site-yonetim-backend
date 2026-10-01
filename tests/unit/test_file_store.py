"""Belge deposu — docs/09 §3: kökün dışına çıkılamaz, üzerine yazılmaz."""

import uuid
from pathlib import Path

import pytest

from site_yonetim.domain.files import FileKind
from site_yonetim.services.files import FileStore

PDF = FileKind("pdf", "application/pdf")


def test_write_read_delete(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    site, file_id = uuid.uuid7(), uuid.uuid7()
    path, digest = store.write(site, file_id, PDF, b"%PDF-1.4")
    assert path == f"{site}/{file_id}.pdf"
    assert len(digest) == 64
    assert store.read(path) == b"%PDF-1.4"
    with pytest.raises(FileExistsError):
        store.write(site, file_id, PDF, b"%PDF-2")  # aynı ad: üzerine yazmaz
    store.delete(path)
    assert not store.exists(path)


@pytest.mark.parametrize("path", ["../disari.pdf", "a/../../disari.pdf"])
def test_paths_outside_root_are_refused(tmp_path: Path, path: str) -> None:
    store = FileStore(tmp_path / "files")
    with pytest.raises(ValueError, match="dışına"):
        store.read(path)
