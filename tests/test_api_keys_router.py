"""
Tests para routers/api_keys.py — CRUD de api_instances, sin ningún test hasta
ahora. Cubre: validación de provider inexistente, y que la key completa nunca
se filtra por list_instances (enmascarado en _safe_instance).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db
from routers import api_keys as api_keys_router


@pytest_asyncio.fixture
async def client(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        # "openrouter" ya viene seedeado por init_db() (SEED_PROVIDERS) — no crearlo de nuevo.
        app = FastAPI()
        app.include_router(api_keys_router.router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_create_instance_rejects_nonexistent_provider(client):
    resp = await client.post("/api/instances", json={
        "id": "inst1", "name": "Test", "provider": "ghost-provider",
        "api_key": "sk-test", "is_free": True,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_instance_rejects_invalid_api_key(client):
    with patch.object(api_keys_router, "_validate_api_key", AsyncMock(return_value="API key inválida (401 Unauthorized)")):
        resp = await client.post("/api/instances", json={
            "id": "inst1", "name": "Test", "provider": "openrouter",
            "api_key": "sk-bad", "is_free": True,
        })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_instances_never_exposes_full_api_key(client):
    with patch.object(api_keys_router, "_validate_api_key", AsyncMock(return_value=None)):
        resp = await client.post("/api/instances", json={
            "id": "inst1", "name": "Test", "provider": "openrouter",
            "api_key": "sk-supersecretvalue", "is_free": True,
        })
        assert resp.status_code == 201

    resp = await client.get("/api/instances")
    assert resp.status_code == 200
    instances = resp.json()
    assert len(instances) == 1
    assert "sk-supersecretvalue" not in instances[0]["api_key"]
    assert instances[0]["api_key"].startswith("sk-s")


@pytest.mark.asyncio
async def test_update_instance_keeps_existing_key_when_blank(client):
    with patch.object(api_keys_router, "_validate_api_key", AsyncMock(return_value=None)):
        await client.post("/api/instances", json={
            "id": "inst1", "name": "Test", "provider": "openrouter",
            "api_key": "sk-original", "is_free": True,
        })
        resp = await client.put("/api/instances/inst1", json={
            "id": "inst1", "name": "Renamed", "provider": "openrouter",
            "api_key": "", "is_free": True,
        })
    assert resp.status_code == 200
    stored = await db.get_instance("inst1")
    assert stored["api_key"] == "sk-original"
    assert stored["name"] == "Renamed"


@pytest.mark.asyncio
async def test_delete_nonexistent_instance_returns_404(client):
    resp = await client.delete("/api/instances/ghost")
    assert resp.status_code == 404
