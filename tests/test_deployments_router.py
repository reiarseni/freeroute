"""
Tests para routers/deployments.py — sin ningún test unitario hasta ahora
(igual que el resto de routers CRUD). Cubre:
  - update_deployment con api_instance_id inexistente (a diferencia de
    create_deployment, no valida — documentar comportamiento real).
  - invalidate_cache() del router se llama tras create/update/delete
    (contrato documentado en CLAUDE.md).
"""

from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db
from routers import deployments as deployments_router


@pytest_asyncio.fixture
async def client_and_dep(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        await db.upsert_instance({
            "id": "inst1", "name": "Test", "provider": "openrouter",
            "api_key": "sk-test", "is_free": 1,
        })
        dep = await db.create_deployment({
            "model_name": "infinity/sonnet", "provider": "openrouter",
            "api_instance_id": "inst1", "model_id": "model-a",
            "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
            "order": 1, "enabled": 1,
        })

        app = FastAPI()
        app.include_router(deployments_router.router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, dep


def _payload(dep: dict, **overrides) -> dict:
    base = {
        "model_name": dep["model_name"],
        "provider": dep["provider"],
        "api_instance_id": dep["api_instance_id"],
        "model_id": dep["model_id"],
        "weight": dep["weight"],
        "rpm": dep["rpm"],
        "tpm": dep["tpm"],
        "max_input_tokens": dep["max_input_tokens"],
        "order": dep["order"],
        "enabled": bool(dep["enabled"]),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_update_deployment_accepts_nonexistent_api_instance_id(client_and_dep):
    """update_deployment (a diferencia de create_deployment) NO valida que
    api_instance_id exista — documenta el comportamiento actual para que una
    corrección futura sea intencional, no un descubrimiento en producción."""
    client, dep = client_and_dep
    resp = await client.put(
        f"/api/deployments/{dep['id']}",
        json=_payload(dep, api_instance_id="does-not-exist"),
    )
    assert resp.status_code == 200

    updated = await db.get_deployments("infinity/sonnet")
    assert updated[0]["api_instance_id"] == "does-not-exist"


@pytest.mark.asyncio
async def test_update_nonexistent_deployment_returns_404(client_and_dep):
    client, dep = client_and_dep
    resp = await client.put("/api/deployments/999999", json=_payload(dep))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_deployment_rejects_nonexistent_api_instance_id(client_and_dep):
    client, dep = client_and_dep
    resp = await client.post(
        "/api/deployments",
        json=_payload(dep, api_instance_id="ghost-instance"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_deployment_invalidates_router_cache(client_and_dep):
    from services.router import router as app_router
    client, dep = client_and_dep
    with patch.object(app_router, "invalidate_cache") as spy:
        resp = await client.post("/api/deployments", json=_payload(dep, model_id="model-b", order=2))
        assert resp.status_code == 201
        spy.assert_called_once()


@pytest.mark.asyncio
async def test_delete_deployment_invalidates_router_cache(client_and_dep):
    from services.router import router as app_router
    client, dep = client_and_dep
    with patch.object(app_router, "invalidate_cache") as spy:
        resp = await client.delete(f"/api/deployments/{dep['id']}")
        assert resp.status_code == 200
        spy.assert_called_once()
