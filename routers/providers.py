"""
REST API — CRUD de providers (data-driven).

Cada provider define base_url, models_url, tipo de auth y headers extra.
Editar/renombrar/borrar aquí reemplaza el antiguo hardcode de
services/provider_handler.py. Al guardar se invalida la caché del router
para que los cambios apliquen sin reiniciar.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db
from services.router import router as app_router

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderIn(BaseModel):
    name: str
    label: str = ""
    base_url: str
    models_url: str
    auth_type: Literal["bearer", "keyless", "static", "oauth_device"] = "bearer"
    auth_value: str = ""
    extra_headers: dict = Field(default_factory=dict)
    kind: str = "plain"


@router.get("")
async def list_providers():
    return await db.get_providers()


@router.post("", status_code=201)
async def create_provider(data: ProviderIn):
    if await db.get_provider(data.name):
        raise HTTPException(409, detail=f"Ya existe un provider '{data.name}'")
    created = await db.create_provider(data.model_dump())
    app_router.invalidate_cache()
    return created


@router.put("/{name}")
async def update_provider(name: str, data: ProviderIn):
    if not await db.get_provider(name):
        raise HTTPException(404, detail=f"Provider '{name}' no encontrado")
    # Si se renombra, el nuevo nombre no debe chocar con otro existente
    if data.name != name and await db.get_provider(data.name):
        raise HTTPException(409, detail=f"Ya existe un provider '{data.name}'")
    updated = await db.update_provider(name, data.model_dump())
    app_router.invalidate_cache()
    return updated


@router.delete("/{name}")
async def delete_provider(name: str):
    if not await db.get_provider(name):
        raise HTTPException(404, detail=f"Provider '{name}' no encontrado")
    # No permitir borrar si hay instancias que lo usan (romperían routing)
    instances = await db.get_all_instances()
    in_use = [i["id"] for i in instances if i["provider"] == name]
    if in_use:
        raise HTTPException(
            409,
            detail=f"Provider '{name}' en uso por instancias: {', '.join(in_use)}",
        )
    await db.delete_provider(name)
    app_router.invalidate_cache()
    return {"ok": True}
