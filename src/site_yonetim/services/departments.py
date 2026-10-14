"""Talep departmanları — frontend servis isteği 11. Açık site kapsamında; transaction'ı çağıran
yönetir.

- Site açılışında varsayılanlar kurulur: Teknik, Temizlik, Güvenlik, Bahçe, Yönetim.
- Ad site içinde benzersiz, Türkçe harf ve büyük/küçük duyarsız (`name_key`).
- Silme yok, pasifleştirme var: pasif departmana yeni talep atanmaz, eskiler yerinde kalır.
- Atama talep geçmişine olay olarak yazılır (`department_changed`). Kişiye atama ayrıdır.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.operations import OperationRuleError, RequestEventKind
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import Request, RequestDepartment, RequestEvent

DEFAULTS = ("Teknik", "Temizlik", "Güvenlik", "Bahçe", "Yönetim")
NAME_MIN, NAME_MAX = 2, 60


def name_key(name: str) -> str:
    return " ".join(ascii_fold(name).split())


def _clean(name: str | None) -> str:
    text = " ".join((name or "").split())
    if not NAME_MIN <= len(text) <= NAME_MAX:
        raise OperationRuleError(
            "invalid_name", f"Departman adı en az {NAME_MIN} karakter.", field="name"
        )
    return text


async def ensure_defaults(session: AsyncSession) -> None:
    """Yeni site: varsayılan departmanlar (varsa dokunmaz)."""
    existing = set(await session.scalars(select(RequestDepartment.name_key)))
    session.add_all(
        RequestDepartment(name=name, name_key=name_key(name), is_active=True)
        for name in DEFAULTS
        if name_key(name) not in existing
    )
    await session.flush()


async def listing(session: AsyncSession) -> list[tuple[RequestDepartment, int]]:
    counts = dict(
        (
            await session.execute(
                select(Request.department_id, func.count())
                .where(Request.department_id.is_not(None))
                .group_by(Request.department_id)
            )
        ).all()
    )
    departments = await session.scalars(select(RequestDepartment).order_by(RequestDepartment.name))
    return [(d, counts.get(d.id, 0)) for d in departments]


async def get(session: AsyncSession, department_id: uuid.UUID) -> RequestDepartment | None:
    found: RequestDepartment | None = await session.scalar(
        select(RequestDepartment).where(RequestDepartment.id == department_id)
    )
    return found


async def _ensure_free(session: AsyncSession, name: str, *, excluding: uuid.UUID | None) -> None:
    query = select(RequestDepartment.name).where(RequestDepartment.name_key == name_key(name))
    if excluding is not None:
        query = query.where(RequestDepartment.id != excluding)
    taken = await session.scalar(query)
    if taken is not None:
        raise OperationRuleError(
            "already_exists", f'"{taken}" adında bir departman var.', conflict=True
        )


async def create(session: AsyncSession, name: str | None) -> RequestDepartment:
    text = _clean(name)
    await _ensure_free(session, text, excluding=None)
    department = RequestDepartment(name=text, name_key=name_key(text), is_active=True)
    session.add(department)
    await session.flush()
    return department


async def update(
    session: AsyncSession,
    department: RequestDepartment,
    *,
    name: str | None,
    is_active: bool | None,
) -> RequestDepartment:
    if name is not None:
        text = _clean(name)
        await _ensure_free(session, text, excluding=department.id)
        department.name, department.name_key = text, name_key(text)
    if is_active is not None:
        department.is_active = is_active
    await session.flush()
    return department


async def names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(RequestDepartment.id, RequestDepartment.name).where(
            RequestDepartment.id.in_(list(ids))
        )
    )
    return dict(rows.all())


async def assign(
    session: AsyncSession, request: Request, department_id: uuid.UUID | None, *, actor: str
) -> RequestDepartment | None:
    department = None
    if department_id is not None:
        department = await get(session, department_id)
        if department is None or not department.is_active:
            raise OperationRuleError(
                "invalid_department", "Geçerli bir departman seçin.", field="department_id"
            )
    request.department_id = department.id if department else None
    session.add(
        RequestEvent(
            request_id=request.id,
            kind=RequestEventKind.DEPARTMENT_CHANGED.value,
            description=f"Departman: {department.name}" if department else "Departman kaldırıldı",
            actor_name=actor,
        )
    )
    await session.flush()
    return department
