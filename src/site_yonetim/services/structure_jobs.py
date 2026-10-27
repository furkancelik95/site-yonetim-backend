"""Yapı paketinin gece işi (`cli run-structure-jobs`, günde bir):

- Tarihi gelen yürürlük tarihli bölüm özniteliklerini uygular (YAP-02 S3).
- Süresi dolan davetleri `expired` yazar (YAP-07: "Davet süresi dolumu — günlük").

İş yapılacak siteler "tüm siteler" kapsamında bulunur; her site kendi transaction'ında işlenir.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.invitations import OPEN_INVITATION
from site_yonetim.models import Invitation, UnitAttributeChange
from site_yonetim.models.audit import Actor, ActorType, set_actor
from site_yonetim.services.invitations import expire_due
from site_yonetim.services.structure import apply_due_attribute_changes

ACTOR = "Sistem (yapı işleri)"


@dataclass(frozen=True, slots=True)
class SiteResult:
    site_id: uuid.UUID
    applied: int
    expired: int


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[SiteResult]:
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(
                await session.scalars(
                    union(
                        select(UnitAttributeChange.site_id).where(
                            UnitAttributeChange.applied.is_(False),
                            UnitAttributeChange.effective_from <= today,
                        ),
                        select(Invitation.site_id).where(
                            Invitation.status.in_([s.value for s in OPEN_INVITATION]),
                            Invitation.expires_at <= now,
                        ),
                    )
                )
            )
    set_actor(Actor(None, ACTOR, None, kind=ActorType.SYSTEM))
    results = []
    for site_id in sorted(site_ids):
        with site_scope(site_id):
            async with factory() as session, session.begin():
                applied = await apply_due_attribute_changes(session, today, now)
                expired = await expire_due(session, now)
        results.append(SiteResult(site_id, applied, expired))
    return results
