"""Yönetim komutları.

uv run python -m site_yonetim.cli seed-demo       # demo verisi (yalnız development)
uv run python -m site_yonetim.cli purge-imports   # süresi dolan Excel aktarımlarını sil (cron)
uv run python -m site_yonetim.cli purge-login-throttle  # dolmuş giriş sayaçlarını sil (gece)
uv run python -m site_yonetim.cli reconcile [--fix]    # özet bakiye ↔ defter mutabakatı (gece)
uv run python -m site_yonetim.cli run-charge-schedules # otomatik aylık tahakkuk (günde bir)
uv run python -m site_yonetim.cli run-recurring-expenses # tekrarlanan giderler (günde bir)
uv run python -m site_yonetim.cli run-approval-jobs      # onay hatırlatma/eskalasyon (saatlik)
uv run python -m site_yonetim.cli run-staff-jobs         # ayrılan personelin erişimini kapat (gece)
uv run python -m site_yonetim.cli run-document-expiry    # belge geçerlilik uyarısı (günde bir)
uv run python -m site_yonetim.cli run-notification-jobs  # bildirim gönderimi, sessiz saat
                                                         # kuyruğu, kritik SMS yedeği (dakikalık)
uv run python -m site_yonetim.cli run-scheduled-reports  # planlı rapor e-postası (dakikalık)
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
    approvals,
    bank_imports,
    charge_schedule,
    delivery,
    documents,
    login_throttle,
    rate_limit,
    reconciliation,
    recurring_expenses,
    scheduled_reports,
    staff,
)
from site_yonetim.services.channels import channels_from_settings
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


async def _run_approval_jobs() -> int:
    """Onay hatırlatma, eskalasyon ve yetki devri başlangıç bildirimi (saatlik)."""
    engine = create_engine_from_settings(get_settings())
    now = datetime.now(UTC)
    try:
        results = await approvals.run_all_timers(
            create_session_factory(engine), now=now, today=now.astimezone(BUSINESS_TZ).date()
        )
    finally:
        await engine.dispose()
    for site_id, r in results:
        print(f"{site_id}: hatırlatma {r.reminded}, eskalasyon {r.escalated}, "
              f"devir {r.delegations_started}")  # fmt: skip
    print(f"{len(results)} sitede onay zamanlayıcısı çalıştı.")
    return 0


async def _run_staff_jobs() -> int:
    """K29 / IK-01 adım 4: ayrılış tarihi geçen personelin site erişimini kapatır (gece)."""
    engine = create_engine_from_settings(get_settings())
    now = datetime.now(UTC)
    try:
        closed = await staff.run_offboarding(
            create_session_factory(engine), today=now.astimezone(BUSINESS_TZ).date(), now=now
        )
    finally:
        await engine.dispose()
    for item in closed:
        print(f"{item.site_id}: {item.full_name} — site erişimi kapatıldı")
    print(f"{len(closed)} ayrılan personelin erişimi kapatıldı.")
    return 0


async def _run_document_expiry() -> int:
    """Belge geçerlilik bitişi (RAP-03): 60/30 gün kala ve bitince bildirim merkezine uyarı;
    günde bir. Tekrar güvenli (aynı uyarı ikinci kez yazılmaz)."""
    engine = create_engine_from_settings(get_settings())
    today = datetime.now(UTC).astimezone(BUSINESS_TZ).date()
    try:
        results = await documents.run_expiry_warnings(create_session_factory(engine), today=today)
    finally:
        await engine.dispose()
    for site_id, written in results:
        print(f"{site_id}: {written} uyarı")
    print(f"{len(results)} sitede belge geçerlilik uyarısı çalıştı.")
    return 0


async def _run_notification_jobs() -> int:
    """Bildirim gönderim katmanı (PLT-05, §4.6): kritik push için 2 dk SMS yedeği, sonra zamanı
    gelen bütün teslimler (sessiz saat kuyruğu dahil). Dakikalık; tekrar güvenli."""
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    now = datetime.now(UTC)
    try:
        factory = create_session_factory(engine)
        fallbacks = await delivery.run_critical_fallbacks(factory, now=now)
        results = await delivery.dispatch_due(
            factory,
            channels_from_settings(settings),
            now=now,
            default_quota=settings.sms_default_monthly_quota,
        )
    finally:
        await engine.dispose()
    for site_id, r in results:
        print(f"{site_id}: gönderildi {r.sent}, başarısız {r.failed}, SMS yedeği {r.fallbacks}")
    print(f"{fallbacks} kritik bildirim için SMS yedeği üretildi; {len(results)} sitede gönderim.")
    return 0


async def _run_scheduled_reports() -> int:
    """Planlı rapor gönderimi (RAP-05): zamanı gelen tanımlar için e-posta kuyruğa alınır ve
    gönderilir. Dakikalık; aynı zaman dilimi iki kez çalışmaz."""
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    now = datetime.now(UTC)
    try:
        factory = create_session_factory(engine)
        results = await scheduled_reports.run_due(factory, now=now, web_url=settings.web_url)
        await delivery.dispatch_due(
            factory,
            channels_from_settings(settings),
            now=now,
            default_quota=settings.sms_default_monthly_quota,
            site_ids=[site_id for site_id, _ in results],
        )
    finally:
        await engine.dispose()
    for site_id, count in results:
        print(f"{site_id}: {count} planlı rapor")
    print(f"{len(results)} sitede planlı rapor çalıştı.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="site_yonetim.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo", help="Demo verisini yükle (yalnız development)")
    sub.add_parser("purge-imports", help="Süresi dolan Excel aktarım dosyalarını sil")
    sub.add_parser("purge-login-throttle", help="Penceresi dolmuş giriş sayaçlarını sil")
    sub.add_parser("run-charge-schedules", help="Otomatik aylık tahakkuku çalıştır (günde bir)")
    sub.add_parser("run-recurring-expenses", help="Tekrarlanan giderleri yaz (günde bir)")
    sub.add_parser("run-approval-jobs", help="Onay hatırlatma ve eskalasyonu (saatlik)")
    sub.add_parser("run-staff-jobs", help="Ayrılan personelin site erişimini kapat (gece)")
    sub.add_parser("run-document-expiry", help="Belge geçerlilik uyarısı (günde bir)")
    sub.add_parser(
        "run-notification-jobs", help="Bildirim gönderimi ve kritik SMS yedeği (dakikalık)"
    )
    sub.add_parser("run-scheduled-reports", help="Planlı rapor e-postaları (dakikalık)")
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
    if args.command == "run-approval-jobs":
        return asyncio.run(_run_approval_jobs())
    if args.command == "run-staff-jobs":
        return asyncio.run(_run_staff_jobs())
    if args.command == "run-document-expiry":
        return asyncio.run(_run_document_expiry())
    if args.command == "run-notification-jobs":
        return asyncio.run(_run_notification_jobs())
    if args.command == "run-scheduled-reports":
        return asyncio.run(_run_scheduled_reports())
    if args.command == "run-recurring-expenses":
        return asyncio.run(_run_recurring_expenses())
    if args.command == "run-charge-schedules":
        return asyncio.run(_run_charge_schedules())
    if args.command == "reconcile":
        return asyncio.run(_reconcile(fix=args.fix))
    return 1  # pragma: no cover — argparse zorunlu alt komut


if __name__ == "__main__":
    raise SystemExit(main())
