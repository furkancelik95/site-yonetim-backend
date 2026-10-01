"""Belge deposu — docs/09 §3. Disk adı sunucu üretir: `{site_id}/{file_id}.{uzantı}`.

- Kök web/statik klasörün dışındadır (`FILE_STORAGE_ROOT`); indirme her zaman yetkili bir uçtan.
- Okunan/yazılan yolun kökün içinde olduğu doğrulanır.
- Kayıt (veritabanı) başarısız olursa yazılan dosya silinir: reddedilen dosyadan iz kalmaz.
"""

import hashlib
import os
import uuid
from pathlib import Path

from site_yonetim.domain.files import FileKind


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, storage_path: str) -> Path:
        path = (self.root / storage_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Depo kökünün dışına erişilemez.")
        return path

    def write(
        self, site_id: uuid.UUID, file_id: uuid.UUID, kind: FileKind, data: bytes
    ) -> tuple[str, str]:
        """Yazar; `(storage_path, sha256)` döner. Var olan dosyanın üzerine yazmaz."""
        storage_path = f"{site_id}/{file_id}.{kind.extension}"
        path = self._path(storage_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return storage_path, hashlib.sha256(data).hexdigest()

    def read(self, storage_path: str) -> bytes:
        return self._path(storage_path).read_bytes()

    def delete(self, storage_path: str) -> None:
        self._path(storage_path).unlink(missing_ok=True)

    def exists(self, storage_path: str) -> bool:
        return self._path(storage_path).is_file()
