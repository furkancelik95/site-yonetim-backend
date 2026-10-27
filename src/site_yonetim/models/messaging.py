"""Bildirim gönderim katmanı — Yazılım Dokümanı PLT-05, ILT-05, RAP-05 (docs/03).

- `notification_deliveries` [K]: dış kanal teslim kaydı (kanal, durum, sağlayıcı referansı,
  maliyet birimi = SMS adedi, kota aşımı). Sessiz saatte `queued` + `scheduled_for` ile bekler;
  kritik bildirimde SMS yedeği `fallback_for_id` ile bağlanır.
- `site_messaging_settings` [K]: sessiz saatler, SMS hesabı (platform kotası / sitenin kendi
  hesabı), gönderici başlığı, aylık kota. Sağlayıcı anahtarı şifreli; ekranda son 4 karakter.
- `scheduled_reports` / `scheduled_report_runs` [K]: planlı rapor tanımı ve çalışma geçmişi.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.delivery import DeliveryStatus, SmsAccountMode
from site_yonetim.domain.notifications import NotificationCategory, NotificationChannel
from site_yonetim.domain.scheduled_reports import ReportFormat, ReportFrequency, ReportRunStatus


class NotificationDelivery(TenantMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = tenant_table_args(
        tenant_fk("notification_id", "notifications"),
        tenant_fk("fallback_for_id", "notification_deliveries"),
        CheckConstraint(enum_check("channel", NotificationChannel), name="channel"),
        CheckConstraint(enum_check("category", NotificationCategory), name="category"),
        CheckConstraint(enum_check("status", DeliveryStatus), name="status"),
        CheckConstraint("cost_units >= 0", name="cost_units"),
        UniqueConstraint("site_id", "notification_id", "channel"),
        Index("ix_notification_deliveries_due", "site_id", "status", "scheduled_for"),
        Index("ix_notification_deliveries_site_created", "site_id", "created_at"),
    )

    notification_id: Mapped[uuid.UUID | None]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text)  # telefon / e-posta / "push"
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)  # gizli bilgi (kod, bağlantı jetonu) içermez
    status: Mapped[str] = mapped_column(Text, default=DeliveryStatus.QUEUED.value)
    is_critical: Mapped[bool] = mapped_column(default=False)
    scheduled_for: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    deferred: Mapped[bool] = mapped_column(default=False)  # sessiz saat nedeniyle ertelendi
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cost_units: Mapped[int] = mapped_column(default=0)  # SMS adedi
    is_overage: Mapped[bool] = mapped_column(default=False)  # kota aşımı (faturaya yansır)
    fallback_for_id: Mapped[uuid.UUID | None]


class MessagingSettings(TenantMixin, Base):
    __tablename__ = "site_messaging_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint(enum_check("sms_mode", SmsAccountMode), name="sms_mode"),
        CheckConstraint("quiet_from <> quiet_to", name="quiet_hours"),
        CheckConstraint("sms_monthly_quota IS NULL OR sms_monthly_quota >= 0", name="quota"),
    )

    quiet_from: Mapped[dt.time] = mapped_column(Time)
    quiet_to: Mapped[dt.time] = mapped_column(Time)
    sms_mode: Mapped[str] = mapped_column(Text, default=SmsAccountMode.PLATFORM.value)
    sms_sender: Mapped[str | None] = mapped_column(Text)  # kendi hesabında başlık
    sms_provider: Mapped[str | None] = mapped_column(Text)
    sms_api_key_sealed: Mapped[str | None] = mapped_column(Text)
    sms_api_key_last4: Mapped[str | None] = mapped_column(Text)
    sms_monthly_quota: Mapped[int | None]  # boş → platform paketi varsayılanı


class ScheduledReport(TenantMixin, Base):
    __tablename__ = "scheduled_reports"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("frequency", ReportFrequency), name="frequency"),
        CheckConstraint(enum_check("format", ReportFormat), name="format"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint(
            "day_of_month IS NULL OR day_of_month BETWEEN 1 AND 28", name="day_of_month"
        ),
        Index("ix_scheduled_reports_due", "site_id", "is_active", "next_run_at"),
    )

    name: Mapped[str] = mapped_column(Text)
    report_key: Mapped[str] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(Text)
    weekday: Mapped[int | None]
    day_of_month: Mapped[int | None]
    send_time: Mapped[dt.time] = mapped_column(Time)
    format: Mapped[str] = mapped_column(Text, default=ReportFormat.PDF.value)
    recipient_user_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    is_active: Mapped[bool] = mapped_column(default=True)
    next_run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_name: Mapped[str] = mapped_column(Text)


class ScheduledReportRun(TenantMixin, Base):
    __tablename__ = "scheduled_report_runs"
    __table_args__ = tenant_table_args(
        tenant_fk("report_id", "scheduled_reports"),
        CheckConstraint(enum_check("status", ReportRunStatus), name="status"),
        UniqueConstraint("site_id", "report_id", "run_at"),
        Index("ix_scheduled_report_runs_report", "site_id", "report_id", "run_at"),
    )

    report_id: Mapped[uuid.UUID]
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    period_label: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    recipient_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
