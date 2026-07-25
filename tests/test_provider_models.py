"""
Tests para routers/provider_models.py — filtro de modelos por proveedor, sin
ningún test hasta ahora. Cubre el filtro `kind == "nvidia"` (skip_prefixes):
lógica frágil por lista hardcodeada — un modelo nuevo no-chat que no matchee
ningún prefijo aparecería como chat model y rompería el flujo real.

Usa TestClient (sync, httpx.Client por debajo) en vez de httpx.AsyncClient +
ASGITransport: el endpoint bajo prueba abre su propio httpx.AsyncClient
interno, y parchear `httpx.AsyncClient.get` también interceptaría la llamada
del cliente de test si este usara la misma clase async.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
from routers import provider_models as provider_models_router


@pytest_asyncio.fixture
async def client(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        # "nvidia" ya viene seedeado por init_db() (SEED_PROVIDERS) — no crearlo de nuevo.
        await db.upsert_instance({
            "id": "nvidia-1", "name": "NVIDIA", "provider": "nvidia",
            "api_key": "sk-test", "is_free": 1,
        })
        # "kiro" ya viene seedeado por init_db() (SEED_PROVIDERS) — no crearlo de nuevo.
        await db.upsert_instance({
            "id": "kiro-1", "name": "Kiro", "provider": "kiro", "api_key": "", "is_free": 1,
            "oauth_state": {"status": "active", "access_token": "tok"},
        })
        app = FastAPI()
        app.include_router(provider_models_router.router)
        with TestClient(app) as c:
            yield c


def _mock_models_response(model_ids: list[str]):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"data": [{"id": mid} for mid in model_ids]})
    return resp


def test_nvidia_filter_excludes_known_non_chat_prefixes(client):
    models = [
        "nvidia/nemotron-3-ultra-550b-a55b",   # chat — debe quedar
        "nvidia/nv-embed-v2",                   # embedding — debe excluirse
        "nvidia/nvclip",                        # vision — debe excluirse
        "baai/bge-m3",                           # embedding — debe excluirse
        "z-ai/glm-5.2",                          # chat de otro vendor — debe quedar
    ]
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_mock_models_response(models))):
        resp = client.get("/api/provider-models", params={"instance_id": "nvidia-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert "nvidia/nemotron-3-ultra-550b-a55b" in data["models"]
    assert "z-ai/glm-5.2" in data["models"]
    assert "nvidia/nv-embed-v2" not in data["models"]
    assert "nvidia/nvclip" not in data["models"]
    assert "baai/bge-m3" not in data["models"]


def test_nvidia_filter_keeps_unknown_prefix_as_chat_by_default(client):
    """Documenta el comportamiento actual: un model_id que no matchee ningún
    skip_prefix pasa como chat model aunque no lo sea (p.ej. un embedding nuevo
    que NVIDIA agregue con un prefijo no listado) — riesgo real señalado en el
    análisis de cobertura, no falso positivo del test."""
    models = ["nvidia/some-brand-new-embed-model-not-in-the-list"]
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_mock_models_response(models))):
        resp = client.get("/api/provider-models", params={"instance_id": "nvidia-1"})

    assert resp.status_code == 200
    assert "nvidia/some-brand-new-embed-model-not-in-the-list" in resp.json()["models"]


def test_provider_models_404_on_unknown_instance(client):
    resp = client.get("/api/provider-models", params={"instance_id": "ghost"})
    assert resp.status_code == 404


def test_kiro_returns_static_model_list_without_http_call(client):
    """Kiro no tiene models_url real: debe devolver la lista estática sin
    disparar ninguna request HTTP saliente."""
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=AssertionError(
        "provider-models no debería hacer ningún GET HTTP para kind=kiro"
    ))):
        resp = client.get("/api/provider-models", params={"instance_id": "kiro-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "kiro"
    assert "auto" in data["models"]
    assert "claude-sonnet-4.5" in data["models"]


def test_provider_models_502_on_upstream_error(client):
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = client.get("/api/provider-models", params={"instance_id": "nvidia-1"})
    assert resp.status_code == 502
