"""
REST API — Estadísticas de latencia del router.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/router-stats", tags=["router-stats"])


@router.get("/latency")
async def get_latency_stats():
    """Retorna latencia avg/p50/p95 por deployment."""
    from services.router import router as app_router

    stats = {}
    for did, dq in app_router._latencies.items():
        if not dq:
            continue
        values = sorted(ms for _, ms in dq)
        n = len(values)
        stats[did] = {
            "samples": n,
            "avg_ms": round(sum(values) / n, 1),
            "p50_ms": round(values[n // 2], 1),
            "p95_ms": round(values[int(n * 0.95)] if n > 1 else values[-1], 1),
        }
    return stats


@router.get("/cooldowns")
async def get_cooldown_stats():
    """Retorna cooldowns activos del router."""
    from services.router import router as app_router
    if app_router._cooldown:
        return app_router._cooldown.get_status()
    return {"active_cooldowns": {}, "failure_counts": {}}


@router.get("/in-flight")
async def get_in_flight():
    """Retorna conteo de in-flight requests por deployment."""
    from services.routing_strategies import _least_busy_strategy
    return _least_busy_strategy._in_flight
