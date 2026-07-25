"""
REST API — CRUD de deployments (reemplaza chain_slots).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


class DeploymentIn(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_name: str
    provider: str = ""
    api_instance_id: str
    model_id: str
    weight: float = 1.0
    rpm: int = 0
    tpm: int = 0
    max_input_tokens: int = 0
    max_parallel_requests: int = 0
    order: int = 0
    enabled: bool = True


@router.get("")
async def list_deployments(model_name: str | None = None):
    return await db.get_deployments(model_name)


@router.get("/model-names")
async def list_model_names():
    return await db.get_distinct_model_names()


@router.post("", status_code=201)
async def create_deployment(data: DeploymentIn):
    # Validar que api_instance_id exista
    instance = await db.get_instance(data.api_instance_id)
    if not instance:
        raise HTTPException(422, detail=f"API instance '{data.api_instance_id}' no existe")
    created = await db.create_deployment(data.model_dump())
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True, "id": created["id"]}


@router.put("/{deployment_id}")
async def update_deployment(deployment_id: int, data: DeploymentIn):
    existing = await db.get_deployments()
    if not any(d["id"] == deployment_id for d in existing):
        raise HTTPException(404, detail=f"Deployment {deployment_id} no encontrado")
    await db.update_deployment(deployment_id, data.model_dump())
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True}


@router.delete("/{deployment_id}")
async def delete_deployment(deployment_id: int):
    existing = await db.get_deployments()
    if not any(d["id"] == deployment_id for d in existing):
        raise HTTPException(404, detail=f"Deployment {deployment_id} no encontrado")
    await db.delete_deployment(deployment_id)
    from services.router import router as app_router
    app_router.invalidate_cache()
    return {"ok": True}
