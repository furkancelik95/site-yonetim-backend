from fastapi import APIRouter

from site_yonetim.api.v1 import auth, health, me, platform, sites, structure

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(sites.router)
api_router.include_router(platform.router)
api_router.include_router(structure.router)
