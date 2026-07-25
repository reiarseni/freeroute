"""
Tests para db.py — capas de acceso a datos sin cubrir: auto-reorder de
deployments y round-trip de extra_headers en providers.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio

import db


@pytest_asyncio.fixture
async def isolated_db(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        yield


@pytest.mark.asyncio
async def test_update_deployment_order_clash_remaps_to_max_plus_one(isolated_db):
    """Dos deployments del mismo model_name no pueden compartir 'order' — si un
    update choca con el order de otro, se remapea a max+1 en vez de violar el
    UNIQUE(model_name, order). Sin este test, una regresión aquí rompe
    silenciosamente el orden de fallback de un tier entero."""
    await db.upsert_instance({
        "id": "inst1", "name": "Test", "provider": "openrouter",
        "api_key": "sk-test", "is_free": 1,
    })
    dep_a = await db.create_deployment({
        "model_name": "infinity/sonnet", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "model-a",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 1, "enabled": 1,
    })
    dep_b = await db.create_deployment({
        "model_name": "infinity/sonnet", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "model-b",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 2, "enabled": 1,
    })

    # Mover dep_b al mismo order que dep_a (1) — debe remapearse, no lanzar excepción.
    updated = await db.update_deployment(dep_b["id"], {
        "model_name": "infinity/sonnet", "order": 1,
    })

    assert updated is not None
    assert updated["order"] != 1
    assert updated["order"] == 3  # max(1, 2) + 1

    deployments = await db.get_deployments("infinity/sonnet")
    orders = sorted(d["order"] for d in deployments)
    assert orders == sorted(set(orders))  # sin duplicados
    assert dep_a["id"] in [d["id"] for d in deployments]


@pytest.mark.asyncio
async def test_update_deployment_no_clash_keeps_requested_order(isolated_db):
    await db.upsert_instance({
        "id": "inst1", "name": "Test", "provider": "openrouter",
        "api_key": "sk-test", "is_free": 1,
    })
    dep = await db.create_deployment({
        "model_name": "infinity/haiku", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "model-a",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 1, "enabled": 1,
    })
    updated = await db.update_deployment(dep["id"], {
        "model_name": "infinity/haiku", "order": 5,
    })
    assert updated["order"] == 5


@pytest.mark.asyncio
async def test_provider_extra_headers_roundtrip(isolated_db):
    """Si el round-trip JSON de extra_headers falla, todos los headers custom
    de un provider se pierden silenciosamente al leer de vuelta."""
    created = await db.create_provider({
        "name": "custom", "label": "Custom", "base_url": "https://x.test/v1",
        "models_url": "https://x.test/v1/models", "auth_type": "bearer",
        "auth_value": "", "kind": "plain",
        "extra_headers": {"HTTP-Referer": "http://localhost:8787", "X-Title": "Infinity"},
    })
    assert created["extra_headers"] == {"HTTP-Referer": "http://localhost:8787", "X-Title": "Infinity"}

    fetched = await db.get_provider("custom")
    assert fetched["extra_headers"] == {"HTTP-Referer": "http://localhost:8787", "X-Title": "Infinity"}

    updated = await db.update_provider("custom", {
        **fetched, "extra_headers": {"X-Custom": "value"},
    })
    assert updated["extra_headers"] == {"X-Custom": "value"}
