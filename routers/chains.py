"""
REST API — DEPRECATED: chain slots.
Los endpoints retornan 410 Gone con mensaje de migración a /api/deployments.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/chains")

_MIGRATION_MSG = "Este endpoint está deprecado. Usá /api/deployments en su lugar."


@router.get("/{chain_id}")
async def get_chain(chain_id: str):
    raise HTTPException(410, detail=_MIGRATION_MSG)


@router.put("/{chain_id}/slots")
async def replace_slots(chain_id: str):
    raise HTTPException(410, detail=_MIGRATION_MSG)
