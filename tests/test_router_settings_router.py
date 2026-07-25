"""
Tests para routers/router_settings.py — sin ningún test hasta ahora. Cubre
la validación de routing_strategy: una regresión aquí permitiría guardar una
strategy inválida que rompe _reload_cache del router en producción.
"""

from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db
from routers import router_settings as router_settings_router


@pytest_asyncio.fixture
async def client(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        app = FastAPI()
        app.include_router(router_settings_router.router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_update_setting_rejects_invalid_routing_strategy(client):
    resp = await client.put("/api/router-settings/routing_strategy", json={
        "key": "routing_strategy", "value": "not-a-real-strategy",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_setting_accepts_valid_routing_strategy_and_invalidates_cache(client):
    from services.router import router as app_router
    with patch.object(app_router, "invalidate_cache") as spy:
        resp = await client.put("/api/router-settings/routing_strategy", json={
            "key": "routing_strategy", "value": "simple-shuffle",
        })
        assert resp.status_code == 200
        spy.assert_called_once()

    settings = await db.get_router_settings()
    assert settings["routing_strategy"] == "simple-shuffle"


@pytest.mark.asyncio
async def test_bulk_update_rejects_invalid_routing_strategy(client):
    resp = await client.put("/api/router-settings", json={
        "settings": {"routing_strategy": "bogus", "hanging_threshold": 5},
    })
    assert resp.status_code == 422
    settings = await db.get_router_settings()
    assert settings.get("hanging_threshold") != 5


@pytest.mark.asyncio
async def test_get_nonexistent_setting_returns_404(client):
    resp = await client.get("/api/router-settings/not-a-real-key")
    assert resp.status_code == 404
