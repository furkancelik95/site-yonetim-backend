"""Yönetim komutları.

uv run python -m site_yonetim.cli seed-demo       # demo verisi (yalnız development)
uv run python -m site_yonetim.cli purge-imports   # süresi dolan Excel aktarımlarını sil (cron)
uv run python -m site_yonetim.cli purge-login-throttle  # dolmuş giriş sayaçlarını sil (gece)
uv run python -m site_yonetim.cli reconcile [--fix]    # özet bakiye ↔ defter mutabakatı (gece)
uv run python -m site_yonetim.cli run-charge-schedules # otomatik aylık tahakkuk (günde bir)
uv run python -m site_yonetim.cli run-recurring-expenses # tekrarlanan giderler (günde bir)
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta

from site_yonetim.core.config import get_settings
from site_yonetim.core.logging import configure_logging
from site_yonetim.db.session import create_engine_from_settings, create_session_factory
from site_yonetim.domain.finance import ScheduleRunStatus
from site_yonetim.seed.demo import DemoSeedRefusedError, ensure_demo_allowed, seed_demo
from site_yonetim.services import (
    bank_imports,
    charge_schedule,
    login_throttle,
    rate_limit,
    reconciliation,
    recurring_expenses,
)
from site_yonetim.services.charging import BUSINESS_TZ
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
    """Onaylanmamış aktarımlar 6 saat sonra silinir (docs/11 §1): Excel daire aktarım dosyaları
    ve banka ekstresi önizlemeleri. Yükleme sırasında da çalışır; trafik yokken temiz tutmak için
    saatlik zamanlanmış iş olarak da çalıştırın."""
    settings = get_settings()
    now = datetime.now(UTC)
    removed = ImportStore(settings.import_dir).purge_expired(now)
    print(f"{removed} süresi dolmuş aktarım dosyası silindi.")
    if settings.database_url is not None:
        removed_bank = asyncio.run(_purge_bank_imports(now))
        print(f"{removed_bank} süresi dolmuş banka ekstresi önizlemesi silindi.")
    return 0


async def _purge_bank_imports(now: datetime) -> int:
    engine = create_engine_from_settings(get_settings())
    try:
        return await bank_imports.purge_expired(create_session_factory(engine), now=now)
    finally:
        await engine.dispose()


async def _purge_login_throttle() -> int:
    """Penceresi dolmuş IP sayaçları (docs/05 §8.1) ve herkese açık uçların istek sınırı
    kayıtları; günde bir kez yeter."""
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    now = datetime.now(UTC)
    try:
        factory = create_session_factory(engine)
        removed = await login_throttle.purge(
            factory, now=now, window=timedelta(minutes=settings.login_ip_window_minutes)
        )
        limits = await rate_limit.purge(factory, now=now, older_than=timedelta(hours=1))
    finally:
        await engine.dispose()
    print(f"{removed} dolmuş giriş sayacı silindi.")
    print(f"{limits} dolmuş istek sınırı kaydı silindi.")
    return 0


logger = logging.getLogger("site_yonetim.reconciliation")


async def _reconcile(*, fix: bool) -> int:
    """Gece mutabakatı (docs/08 §2). Fark varsa her biri ERROR olarak loglanır (alarm) ve çıkış
    kodu 1 olur; `--fix` farklı sitelerin özetlerini defterden yeniden üretir."""
    engine = create_engine_from_settings(get_settings())
    factory = create_session_factory(engine)
    try:
        found = await reconciliation.find_mismatches(factory)
        for m in found:
            logger.error(
                "Mutabakat tutmadı: %s site_id=%s anahtar=%s alan=%s defter=%s özet=%s",
                m.kind, m.site_id, m.key, m.field, m.expected, m.actual,
            )  # fmt: skip
        if not found:
            print("Mutabakat tuttu: fark yok.")
            return 0
        print(f"{len(found)} fark bulundu.")
        if not fix:
            return 1
        sites = {m.site_id for m in found}
        await reconciliation.repair(factory, sites)
        remaining = await reconciliation.find_mismatches(factory)
    finally:
        await engine.dispose()
    if remaining:
        print(f"Onarımdan sonra {len(remaining)} fark kaldı.")
        return 1
    print(f"{len(sites)} sitenin özetleri defterden yeniden üretildi; onarıldı.")
    return 0


async def _run_charge_schedules() -> int:
    """Otomatik aylık tahakkuk (servis isteği 04); günde bir, gece. Başarısız site varsa 1."""
    engine = create_engine_from_settings(get_settings())
    now = datetime.now(UTC)
    try:
        results = await charge_schedule.run_all(
            create_session_factory(engine), today=now.astimezone(BUSINESS_TZ).date(), now=now
        )
    finally:
        await engine.dispose()
    for result in results:
        outcome = result.outcome
        print(f"{result.site_id}: {outcome.status.value} {outcome.message or ''}".rstrip())
    print(f"{len(results)} sitede otomatik tahakkuk çalıştı.")
    failed = any(r.outcome.status is ScheduleRunStatus.FAILED for r in results)
    return 1 if failed else 0


async def _run_recurring_expenses() -> int:
    """Tekrarlanan giderler (servis isteği 07); günde bir, gece. Başarısız tanım varsa 1."""
    engine = create_engine_from_settings(get_settings())
    now = datetime.now(UTC)
    try:
        results = await recurring_expenses.run_all(
            create_session_factory(engine), today=now.astimezone(BUSINESS_TZ).date(), now=now
        )
    finally:
        await engine.dispose()
    for site_id, outcome in results:
        print(f"{site_id} {outcome.item_id}: {outcome.status} {outcome.message or ''}".rstrip())
    print(f"{len(results)} tekrarlanan gider işlendi.")
    return 1 if any(o.status == "failed" for _, o in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="site_yonetim.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="Demo verisini yükle (yalnız development)")
    sub.add_parser("purge-imports", help="Süresi dolan Excel aktarım dosyalarını sil")
    sub.add_parser("purge-login-throttle", help="Penceresi dolmuş giriş sayaçlarını sil")
    sub.add_parser("run-charge-schedules", help="Otomatik aylık tahakkuku çalıştır (günde bir)")
    sub.add_parser("run-recurring-expenses", help="Tekrarlanan giderleri yaz (günde bir)")
    reconcile = sub.add_parser("reconcile", help="Özet bakiyeleri defterle karşılaştır")
    reconcile.add_argument("--fix", action="store_true", help="Farklı siteleri onar")
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.command == "seed-demo":
        return asyncio.run(_seed_demo())
    if args.command == "purge-imports":
        return _purge_imports()
    if args.command == "purge-login-throttle":
        return asyncio.run(_purge_login_throttle())
    if args.command == "run-recurring-expenses":
        return asyncio.run(_run_recurring_expenses())
    if args.command == "run-charge-schedules":
        return asyncio.run(_run_charge_schedules())
    if args.command == "reconcile":
        return asyncio.run(_reconcile(fix=args.fix))
    return 1  # pragma: no cover — argparse zorunlu alt komut


if __name__ == "__main__":
    raise SystemExit(main())
