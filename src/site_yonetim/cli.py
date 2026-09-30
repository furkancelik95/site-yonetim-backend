"""Yönetim komutları.

uv run python -m site_yonetim.cli seed-demo       # demo verisi (yalnız development)
uv run python -m site_yonetim.cli purge-imports   # süresi dolan Excel aktarımlarını sil (cron)
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from site_yonetim.core.config import get_settings
from site_yonetim.core.logging import configure_logging
from site_yonetim.db.session import create_engine_from_settings, create_session_factory
from site_yonetim.seed.demo import DemoSeedRefusedError, ensure_demo_allowed, seed_demo
from site_yonetim.services.imports import ImportStore


async def _seed_demo() -> int:
    settings = get_settings()
    try:
        ensure_demo_allowed(settings)
    except DemoSeedRefusedError as exc:
        print(exc, file=sys.stderr)
        return 2
    engine = create_engine_from_settings(settings)
    try:
        created = await seed_demo(settings, create_session_factory(engine))
    finally:
        await engine.dispose()
    print("Demo verisi yüklendi." if created else "Demo verisi zaten var; değişiklik yok.")
    return 0


def _purge_imports() -> int:
    """Onaylanmamış aktarımlar 6 saat sonra silinir (docs/11 §1). Yükleme sırasında da çalışır;
    trafik yokken diski temiz tutmak için saatlik zamanlanmış iş olarak da çalıştırın."""
    removed = ImportStore(get_settings().import_dir).purge_expired(datetime.now(UTC))
    print(f"{removed} süresi dolmuş aktarım dosyası silindi.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="site_yonetim.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="Demo verisini yükle (yalnız development)")
    sub.add_parser("purge-imports", help="Süresi dolan Excel aktarım dosyalarını sil")
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.command == "seed-demo":
        return asyncio.run(_seed_demo())
    if args.command == "purge-imports":
        return _purge_imports()
    return 1  # pragma: no cover — argparse zorunlu alt komut


if __name__ == "__main__":
    raise SystemExit(main())
