"""Bildirim motoru — Yazılım Dokümanı PLT-05. Modüller doğrudan SMS/e-posta göndermez; bu
servise "şu kişilere şu olay" der.

- Uygulama içi bildirim **aynı transaction'da** yazılır (olayı üreten kayıtla birlikte; işlem
  geri alınırsa bildirim de alınır). Açık site kapsamında; transaction'ı çağıran yönetir.
- Tekrar güvenli: aynı tekil anahtarla aynı kişiye ikinci bildirim üretilmez (benzersiz kısıt +
  `ON CONFLICT DO NOTHING`; olay iki kez yayınlansa da tek bildirim).
- Dış kanallar (push, SMS, e-posta): yeni yazılan her bildirim için gönderim katmanı
  (`services/delivery.py`, NotificationService) aynı transaction'da teslim kaydı üretir —
  kişinin kanal tercihi, adresi ve sitenin sessiz saatleri uygulanır. Sağlayıcı K5'te seçilene
  dek test sağlayıcısı: gönderim kayda geçer, dışarı gitmez.
- 90 günden eski bildirim listede görünmez (saklama ayrı: K8).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope
from site_yonetim.domain.notifications import (
    Matrix,
    NotificationCategory,
    NotificationChannel,
    Overrides,
    PushPlatform,
    apply_changes,
    preference_matrix,
)
from site_yonetim.models import Notification, NotificationPreference, PushToken
from site_yonetim.services import delivery

LIST_WINDOW = timedelta(days=90)
TITLE_MAX, BODY_MAX = 200, 1000


@dataclass(frozen=True, slots=True)
class Message:
    category: NotificationCategory
    kind: str  # ör. "approval.requested" — frontend ikon ve metin seçer
    title: str
    body: str | None = None
    link_entity: str | None = None
    link_id: uuid.UUID | None = None


async def notify(
    session: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    message: Message,
    *,
    dedupe_key: str,
    now: datetime | None = None,
) -> int:
    """Kişilere uygulama içi bildirim yazar ve dış kanal teslimlerini planlar; yeni yazılan
    sayısını döner (tekrarlar sayılmaz — tekrar eden bildirime ikinci SMS de gitmez)."""
    recipients = sorted(set(user_ids))
    if not recipients:
        return 0
    scope = current_scope()
    if scope is None or scope.site_id is None:  # pragma: no cover - programlama hatası
        raise RuntimeError("Bildirim site kapsamında yazılır.")
    rows = [
        {
            "id": uuid.uuid7(),
            "site_id": scope.site_id,
            "user_id": user_id,
            "category": message.category.value,
            "kind": message.kind,
            "title": message.title[:TITLE_MAX],
            "body": message.body[:BODY_MAX] if message.body else None,
            "link_entity": message.link_entity,
            "link_id": message.link_id,
            "dedupe_key": dedupe_key,
        }
        for user_id in recipients
    ]
    result = await session.execute(
        insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["site_id", "user_id", "dedupe_key"])
        .returning(Notification.id, Notification.user_id)
    )
    written = [(notification_id, user_id) for notification_id, user_id in result.all()]
    await delivery.plan(
        session,
        written,
        delivery.Planned(message.category, message.kind, message.title, message.body),
        now=now,
    )
    return len(written)


def list_query(
    user_id: uuid.UUID,
    *,
    now: datetime,
    unread: bool = False,
    category: NotificationCategory | None = None,
) -> Select[Notification]:
    query = select(Notification).where(
        Notification.user_id == user_id, Notification.created_at >= now - LIST_WINDOW
    )
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if category is not None:
        query = query.where(Notification.category == category.value)
    return query.order_by(Notification.created_at.desc(), Notification.id.desc())


async def unread_count(session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> int:
    count = await session.scalar(
        select(func.count()).select_from(list_query(user_id, now=now, unread=True).subquery())
    )
    return count or 0


async def mark_read(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime,
    ids: list[uuid.UUID] | None,
) -> int:
    """`ids` verilirse onları, verilmezse kişinin okunmamışlarının hepsini okundu yapar."""
    conditions = [Notification.user_id == user_id, Notification.read_at.is_(None)]
    if ids is not None:
        conditions.append(Notification.id.in_(ids))
    result = await session.execute(
        update(Notification).where(*conditions).values(read_at=now).returning(Notification.id)
    )
    return len(result.all())


# --- Tercihler ve push jetonları (kişiye ait, siteden bağımsız) -----------------------


async def preferences(session: AsyncSession, user_id: uuid.UUID) -> Matrix:
    rows = await session.scalars(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    overrides: Overrides = {
        (NotificationCategory(r.category), NotificationChannel(r.channel)): r.enabled for r in rows
    }
    return preference_matrix(overrides)


async def update_preferences(
    session: AsyncSession, user_id: uuid.UUID, changes: Overrides
) -> Matrix:
    """Kurallara uymayan değişiklik `PreferenceError` (Türkçe mesaj) fırlatır, hiçbiri yazılmaz."""
    result = apply_changes(await preferences(session, user_id), changes)
    defaults = preference_matrix({})
    for category, channels in result.items():
        for channel, enabled in channels.items():
            statement = insert(NotificationPreference).values(
                id=uuid.uuid7(),
                user_id=user_id,
                category=category.value,
                channel=channel.value,
                enabled=enabled,
            )
            if enabled == defaults[category][channel]:
                # varsayılana dönen hücre saklanmaz
                await session.execute(
                    delete(NotificationPreference).where(
                        NotificationPreference.user_id == user_id,
                        NotificationPreference.category == category.value,
                        NotificationPreference.channel == channel.value,
                    )
                )
                continue
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["user_id", "category", "channel"],
                    set_={"enabled": enabled},
                )
            )
    return result


async def register_push_token(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    token: str,
    platform: PushPlatform,
    now: datetime,
) -> None:
    """Aynı jeton başka kullanıcıyla gelirse bu kullanıcıya taşınır (cihazda hesap değişti)."""
    await session.execute(
        insert(PushToken)
        .values(
            id=uuid.uuid7(),
            user_id=user_id,
            token=token,
            platform=platform.value,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["token"],
            set_={"user_id": user_id, "platform": platform.value, "last_seen_at": now},
        )
    )


async def remove_push_token(session: AsyncSession, user_id: uuid.UUID, token: str) -> bool:
    result = await session.execute(
        delete(PushToken)
        .where(PushToken.user_id == user_id, PushToken.token == token)
        .returning(PushToken.id)
    )
    return bool(result.all())


async def unread_by_site(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    site_ids: list[uuid.UUID],
    *,
    now: datetime,
) -> dict[uuid.UUID, int]:
    """Kişinin erişebildiği sitelerdeki okunmamış sayıları (site seçici rozeti). Tüm-siteler
    kapsamı bilinçli: yalnız kişinin **kendi** bildirimleri ve erişimi olan siteler sayılır."""
    if not site_ids:
        return {}
    with all_sites_scope():
        async with factory() as session:
            rows = await session.execute(
                select(Notification.site_id, func.count())
                .where(
                    Notification.user_id == user_id,
                    Notification.site_id.in_(site_ids),
                    Notification.read_at.is_(None),
                    Notification.created_at >= now - LIST_WINDOW,
                )
                .group_by(Notification.site_id)
            )
            return {site_id: int(count) for site_id, count in rows.all()}
