"""
REST API — Query de request logs.
"""

from fastapi import APIRouter, Query

import db
from services.router import router as app_router

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def get_logs(limit: int = Query(default=100, le=500)):
    logs = await db.get_logs(limit)
    return logs


@router.get("/active")
async def get_active_logs():
    """Peticiones de cliente en curso, aún sin fila en request_logs."""
    return app_router.get_active_requests()
