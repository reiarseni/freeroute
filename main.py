"""
FreeRoute
=========
Dos proxies en paralelo:
  - Puerto 8787: OpenAI-compatible proxy + REST API + SPA frontend
  - Puerto 8788: Anthropic-compatible proxy (para Claude Code)

Arrancar: python3 main.py
"""

import os
import signal
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
from routers import (
    anthropic_proxy,
    api_keys,
    chains,
    deployments,
    logs,
    oauth,
    openai_proxy,
    provider_models,
    providers,
    router_settings,
    router_stats,
)

# ── App 1: OpenAI proxy + API + frontend (puerto 8787) ──────────────────────

@asynccontextmanager
async def lifespan_openai(app: FastAPI):
    await db.init_db()
    from services.router import router as app_router
    await app_router._ensure_init()
    yield
    await app_router.aclose()


openai_app = FastAPI(title="FreeRoute", lifespan=lifespan_openai)

openai_app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8787", "http://127.0.0.1:8787",
                   "http://localhost:8788", "http://127.0.0.1:8788"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Proxy
openai_app.include_router(openai_proxy.router)

# REST API para el frontend
openai_app.include_router(api_keys.router)
openai_app.include_router(chains.router, include_in_schema=False)  # deprecado (410 Gone)
openai_app.include_router(logs.router)
openai_app.include_router(provider_models.router)
openai_app.include_router(providers.router)
openai_app.include_router(deployments.router)
openai_app.include_router(router_settings.router)
openai_app.include_router(router_stats.router)
openai_app.include_router(oauth.router)


@openai_app.get("/api/health")
async def health():
    from services.router import router as app_router
    cooldown_status = app_router._cooldown.get_status() if app_router._cooldown else {}
    stats = await db.get_health_stats()
    return {
        "status": "ok",
        "router": {
            "strategy": app_router._settings_cache.get("routing_strategy", "positional"),
            "cache_dirty": app_router._cache_dirty,
        },
        "cooldowns": cooldown_status,
        "logs_24h": stats["logs_24h"],
    }


# Servir la SPA de Svelte
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    openai_app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @openai_app.get("/", include_in_schema=False)
    @openai_app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str = ""):
        # Las rutas /v1/* y /api/* ya están capturadas arriba — esto solo llega al frontend
        index = STATIC_DIR / "index.html"
        if index.exists():
            # no-cache para que el browser siempre pida el HTML fresco
            # (los assets JS/CSS tienen hash en el nombre — esos sí se pueden cachear)
            return FileResponse(
                index,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return {"message": "Frontend no compilado. Corré: cd frontend && npm run build"}


# ── App 2: Anthropic proxy (puerto 8788) ────────────────────────────────────

@asynccontextmanager
async def lifespan_anthropic(app: FastAPI):
    await db.init_db()
    yield


anthropic_app = FastAPI(title="FreeRoute — Anthropic Proxy", lifespan=lifespan_anthropic)

anthropic_app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8787", "http://127.0.0.1:8787",
                   "http://localhost:8788", "http://127.0.0.1:8788"],
    allow_methods=["*"],
    allow_headers=["*"],
)

anthropic_app.include_router(anthropic_proxy.router)


# ── Entry point ──────────────────────────────────────────────────────────────

# Puertos y host configurables por env; defaults estables.
HOST = os.getenv("FREEROUTE_HOST", "0.0.0.0")
PORT_OPENAI = int(os.getenv("FREEROUTE_PORT_OPENAI", "8787"))
PORT_ANTHROPIC = int(os.getenv("FREEROUTE_PORT_ANTHROPIC", "8788"))


def run_openai():
    uvicorn.run(openai_app, host=HOST, port=PORT_OPENAI, log_level="warning")


def run_anthropic():
    uvicorn.run(anthropic_app, host=HOST, port=PORT_ANTHROPIC, log_level="warning")


def free_port(port: int) -> None:
    """Mata cualquier proceso que esté usando el puerto dado."""
    import subprocess
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"], capture_output=True, text=True
    )
    pids = result.stdout.strip().split()
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    if pids:
        print(f"  Puerto {port} liberado (PID {', '.join(pids)})")


if __name__ == "__main__":
    free_port(PORT_OPENAI)
    free_port(PORT_ANTHROPIC)
    print("FreeRoute arrancando...")
    print(f"  OpenAI proxy + UI: http://localhost:{PORT_OPENAI}")
    print(f"  Anthropic proxy:   http://localhost:{PORT_ANTHROPIC}")

    t_openai = threading.Thread(target=run_openai, daemon=True)
    t_anthropic = threading.Thread(target=run_anthropic, daemon=True)

    t_openai.start()
    t_anthropic.start()

    try:
        t_openai.join()
    except KeyboardInterrupt:
        print("\nDeteniendo...")
