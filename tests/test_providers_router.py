"""
Tests para routers/providers.py — CRUD de providers, sin ningún test hasta
ahora. Cubre la regla de integridad crítica: no permitir borrar un provider
en uso por instancias (rompería el routing de todos sus deployments).
"""

from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db
from routers import providers as providers_router


@pytest_asyncio.fixture
async def client(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        app = FastAPI()
        app.include_router(providers_router.router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _provider_payload(**overrides):
    base = {
        "name": "custom", "label": "Custom", "base_url": "https://x.test/v1",
        "models_url": "https://x.test/v1/models", "auth_type": "bearer",
        "auth_value": "", "extra_headers": {}, "kind": "plain",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_delete_provider_blocked_when_instance_uses_it(client):
    resp = await client.post("/api/providers", json=_provider_payload())
    assert resp.status_code == 201

    await db.upsert_instance({
        "id": "inst1", "name": "Test", "provider": "custom",
        "api_key": "sk-test", "is_free": 1,
    })

    resp = await client.delete("/api/providers/custom")
    assert resp.status_code == 409

    assert await db.get_provider("custom") is not None


@pytest.mark.asyncio
async def test_delete_provider_succeeds_when_unused(client):
    await client.post("/api/providers", json=_provider_payload(name="unused"))
    resp = await client.delete("/api/providers/unused")
    assert resp.status_code == 200
    assert await db.get_provider("unused") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_provider_returns_404(client):
    resp = await client.delete("/api/providers/ghost")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_provider_duplicate_name_returns_409(client):
    await client.post("/api/providers", json=_provider_payload())
    resp = await client.post("/api/providers", json=_provider_payload())
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_provider_rename_clash_returns_409(client):
    await client.post("/api/providers", json=_provider_payload(name="a"))
    await client.post("/api/providers", json=_provider_payload(name="b"))
    resp = await client.put("/api/providers/a", json=_provider_payload(name="b"))
    assert resp.status_code == 409
