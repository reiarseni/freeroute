"""
REST API — CRUD de API instances.
"""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from services.provider_handler import build_handler, parse_oauth_state

router = APIRouter(prefix="/api/instances", tags=["api-keys"])


class InstanceIn(BaseModel):
    id: str
    name: str
    provider: str  # referencia a providers.name (validado contra DB)
    api_key: str
    is_free: bool = True


async def _validate_api_key(provider: str, api_key: str) -> str | None:
    """Valida la API key contra el proveedor. Retorna None si válida o error msg.

    Construye el handler desde la config del provider en DB (data-driven).
    """
    cfg = await db.get_provider(provider)
    if not cfg or not cfg.get("models_url"):
        return None
    handler = build_handler(cfg)
    url = handler.models_url
    # Headers según auth_type del provider (bearer usa la key, keyless/static no)
    headers = await handler._headers({"api_key": api_key})
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 401:
                return "API key inválida (401 Unauthorized)"
            if resp.status_code == 403:
                return "API key sin permisos (403 Forbidden)"
            if resp.status_code >= 500:
                return None
            return None
    except Exception:
        return None


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def _safe_instance(inst: dict) -> dict:
    out = {**inst, "api_key": _mask_key(inst["api_key"])}
    state = parse_oauth_state(inst)
    out["oauth_status"] = state.get("status") if state else None
    out.pop("oauth_state", None)
    return out


@router.get("")
async def list_instances():
    instances = await db.get_all_instances()
    return [_safe_instance(i) for i in instances]


@router.post("", status_code=201)
async def create_instance(data: InstanceIn):
    existing = await db.get_instance(data.id)
    if existing:
        raise HTTPException(409, detail=f"Ya existe una instancia con id '{data.id}'")
    cfg = await db.get_provider(data.provider)
    if not cfg:
        raise HTTPException(422, detail=f"Provider '{data.provider}' no existe")
    # oauth_device no se da de alta pegando una API key: el estado real lo
    # completa el device flow (routers/oauth.py).
    if cfg.get("auth_type") != "oauth_device":
        err = await _validate_api_key(data.provider, data.api_key)
        if err:
            raise HTTPException(422, detail=err)
    await db.upsert_instance({
        "id": data.id,
        "name": data.name,
        "provider": data.provider,
        "api_key": data.api_key,
        "is_free": 1 if data.is_free else 0,
    })
    return {"ok": True, "id": data.id}


@router.put("/{instance_id}")
async def update_instance(instance_id: str, data: InstanceIn):
    existing = await db.get_instance(instance_id)
    if not existing:
        raise HTTPException(404, detail=f"Instancia '{instance_id}' no encontrada")
    cfg = await db.get_provider(data.provider)
    if not cfg:
        raise HTTPException(422, detail=f"Provider '{data.provider}' no existe")
    api_key = data.api_key if data.api_key.strip() else existing["api_key"]
    if cfg.get("auth_type") != "oauth_device" and data.api_key.strip():
        err = await _validate_api_key(data.provider, api_key)
        if err:
            raise HTTPException(422, detail=err)
    await db.upsert_instance({
        "id": instance_id,
        "name": data.name,
        "provider": data.provider,
        "api_key": api_key,
        "is_free": 1 if data.is_free else 0,
    })
    return {"ok": True}


@router.delete("/{instance_id}")
async def delete_instance(instance_id: str):
    existing = await db.get_instance(instance_id)
    if not existing:
        raise HTTPException(404, detail=f"Instancia '{instance_id}' no encontrada")
    await db.delete_instance(instance_id)
    return {"ok": True}
