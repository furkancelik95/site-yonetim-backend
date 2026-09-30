"""Yönetim komutları.

uv run python -m site_yonetim.cli seed-demo    # demo verisi (yalnız development)
"""

import argparse
import asyncio
import sys

from site_yonetim.core.config import get_settings
from site_yonetim.core.logging import configure_logging
from site_yonetim.db.session import create_engine_from_settings, create_session_factory
from site_yonetim.seed.demo import DemoSeedRefusedError, ensure_demo_allowed, seed_demo


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="site_yonetim.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="Demo verisini yükle (yalnız development)")
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.command == "seed-demo":
        return asyncio.run(_seed_demo())
    return 1  # pragma: no cover — argparse zorunlu alt komut


if __name__ == "__main__":
    raise SystemExit(main())
