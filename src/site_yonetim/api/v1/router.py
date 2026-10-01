from fastapi import APIRouter

from site_yonetim.api.v1 import (
    announcements,
    auth,
    budget,
    charges,
    health,
    imports,
    me,
    payments,
    platform,
    requests,
    sites,
    structure,
)

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(sites.router)
api_router.include_router(platform.router)
api_router.include_router(structure.router)
api_router.include_router(budget.router)
api_router.include_router(charges.router)
api_router.include_router(imports.router)
api_router.include_router(payments.router)
api_router.include_router(requests.router)
api_router.include_router(announcements.router)
