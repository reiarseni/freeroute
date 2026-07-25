"""
REST API — CRUD de router settings (fallbacks, strategies, limits).
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from services.routing_strategies import ROUTING_STRATEGIES

router = APIRouter(prefix="/api/router-settings", tags=["router-settings"])

ALLOWED_STRATEGIES = set(ROUTING_STRATEGIES.keys())


class SettingIn(BaseModel):
    key: str
    value: Any


class BulkSettingsIn(BaseModel):
    settings: dict[str, Any]


@router.get("")
async def list_settings():
    """Lista todos los settings con defaults ya aplicados."""
    return await db.get_router_settings()


@router.get("/{key}")
async def get_setting(key: str):
    settings = await db.get_router_settings()
    if key not in settings:
        raise HTTPException(404, detail=f"Setting '{key}' no encontrado")
    return {"key": key, "value": settings[key]}


@router.put("/{key}")
async def update_setting(key: str, data: SettingIn):
    if key == "routing_strategy" and data.value not in ALLOWED_STRATEGIES:
        raise HTTPException(
            422,
            detail=f"routing_strategy inválido. Disponibles: {list(ALLOWED_STRATEGIES)}",
        )
    await db.upsert_router_setting(key, data.value)
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True, "key": key}


@router.put("")
async def bulk_update_settings(data: BulkSettingsIn):
    if "routing_strategy" in data.settings:
        if data.settings["routing_strategy"] not in ALLOWED_STRATEGIES:
            raise HTTPException(
                422,
                detail=f"routing_strategy inválido. Disponibles: {list(ALLOWED_STRATEGIES)}",
            )
    await db.bulk_upsert_router_settings(data.settings)
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True}


@router.delete("/{key}")
async def delete_setting(key: str):
    settings = await db.get_router_settings()
    if key not in settings:
        raise HTTPException(404, detail=f"Setting '{key}' no encontrado")
    await db.delete_router_setting(key)
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True}
