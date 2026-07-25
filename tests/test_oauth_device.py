"""
Tests para OAuth 2.0 Device Authorization Grant (RFC 8628): refresh perezoso +
locking por instancia en services/router.py, y los endpoints de device flow en
routers/oauth.py. Todo mockeado — no toca la red real de AWS SSO OIDC.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db
from routers import oauth as oauth_router
from services.cooldown import CooldownMechanism
from services.provider_handler import OAuthReauthRequired, get_handler
from services.router import Deployment, Router

EXPIRED_STATE = {
    "client_id": "cid", "client_secret": "csecret",
    "refresh_token": "rt-1", "access_token": "at-1",
    "expires_at": "2000-01-01T00:00:00+00:00",
    "status": "active",
}


def _fresh_state(**overrides) -> dict:
    base = {**EXPIRED_STATE, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    base.update(overrides)
    return base


@pytest_asyncio.fixture
async def router_with_oauth_instance(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        await db.upsert_instance({
            "id": "kiro1", "name": "Kiro Test", "provider": "kiro",
            "api_key": "", "is_free": 1, "oauth_state": EXPIRED_STATE,
        })
        await db.create_deployment({
            "model_name": "infinity/sonnet", "provider": "kiro",
            "api_instance_id": "kiro1", "model_id": "claude-sonnet-4",
            "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
            "order": 1, "enabled": 1,
        })
        r = Router()
        await r._ensure_init()
        r._cooldown = CooldownMechanism(persist_path=tmp_path / "cooldowns.json")
        yield r
        await r.aclose()


# ── Refresh perezoso (8.2) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_token_skips_refresh(router_with_oauth_instance):
    r = router_with_oauth_instance
    await db.set_oauth_state("kiro1", _fresh_state())
    instance = await db.get_instance("kiro1")
    handler = get_handler("kiro")

    with patch("services.router.oauth_refresh_token", AsyncMock()) as mock_refresh:
        result = await r._ensure_oauth_fresh(instance, handler)

    mock_refresh.assert_not_called()
    assert result is not None and result["id"] == "kiro1"


@pytest.mark.asyncio
async def test_expired_token_triggers_refresh_and_persists(router_with_oauth_instance):
    r = router_with_oauth_instance
    instance = await db.get_instance("kiro1")  # EXPIRED_STATE ya vencido
    handler = get_handler("kiro")
    token_resp = {"accessToken": "at-2", "refreshToken": "rt-2", "expiresIn": 3600}

    with patch("services.router.oauth_refresh_token", AsyncMock(return_value=token_resp)) as mock_refresh:
        result = await r._ensure_oauth_fresh(instance, handler)

    mock_refresh.assert_awaited_once()
    assert result is not None
    fresh = await db.get_instance("kiro1")
    state = db.get_instance_oauth_state(fresh)
    assert state["access_token"] == "at-2"
    assert state["refresh_token"] == "rt-2"
    assert state["status"] == "active"


# ── Locking por instancia (8.3) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_only_one_real_refresh(router_with_oauth_instance):
    r = router_with_oauth_instance
    instance = await db.get_instance("kiro1")
    handler = get_handler("kiro")
    call_count = 0

    async def slow_refresh(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"accessToken": "at-3", "refreshToken": "rt-3", "expiresIn": 3600}

    with patch("services.router.oauth_refresh_token", slow_refresh):
        results = await asyncio.gather(*[
            r._ensure_oauth_fresh(instance, handler) for _ in range(5)
        ])

    assert call_count == 1
    assert all(res is not None for res in results)
    fresh = await db.get_instance("kiro1")
    assert db.get_instance_oauth_state(fresh)["access_token"] == "at-3"


# ── needs_reauth (8.4) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_rejected_marks_needs_reauth_and_excludes(router_with_oauth_instance):
    r = router_with_oauth_instance
    dep = Deployment.from_row(r._deployments_cache[0])

    with patch("services.router.oauth_refresh_token",
               AsyncMock(side_effect=OAuthReauthRequired("invalid_grant"))):
        result = await r._try_deployment(dep, {"messages": []}, stream=False)

    assert result is None
    fresh = await db.get_instance("kiro1")
    assert db.get_instance_oauth_state(fresh)["status"] == "needs_reauth"
    # Persistente, no transitorio: no debe pasar por el cooldown genérico.
    assert not r._cooldown.is_cooling_down(dep.id)


@pytest.mark.asyncio
async def test_needs_reauth_instance_excluded_without_refresh_attempt(router_with_oauth_instance):
    r = router_with_oauth_instance
    await db.set_oauth_state("kiro1", {**EXPIRED_STATE, "status": "needs_reauth"})
    dep = Deployment.from_row(r._deployments_cache[0])

    with patch("services.router.oauth_refresh_token", AsyncMock()) as mock_refresh:
        result = await r._try_deployment(dep, {"messages": []}, stream=False)

    mock_refresh.assert_not_called()
    assert result is None


# ── Endpoints routers/oauth.py (8.5) ─────────────────────────────────────────


@pytest_asyncio.fixture
async def oauth_test_client(tmp_path):
    test_db = tmp_path / "test.db"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        fresh_router = Router()
        await fresh_router._ensure_init()
        with patch("routers.oauth.app_router", fresh_router):
            app = FastAPI()
            app.include_router(oauth_router.router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        await fresh_router.aclose()


_REGISTER_RESP = {"clientId": "cid", "clientSecret": "csec"}
_AUTHZ_RESP = {
    "deviceCode": "dc1", "userCode": "ABCD-EFGH",
    "verificationUriComplete": "https://verify.test/ABCD-EFGH",
    "interval": 5, "expiresIn": 600,
}


@pytest.mark.asyncio
async def test_start_flow_returns_user_code(oauth_test_client):
    c = oauth_test_client
    with patch("routers.oauth.oauth_register_client", AsyncMock(return_value=_REGISTER_RESP)), \
         patch("routers.oauth.oauth_start_device_authorization", AsyncMock(return_value=_AUTHZ_RESP)):
        resp = await c.post("/api/oauth/kiro/start", json={"name": "mi-cuenta"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_code"] == "ABCD-EFGH"
    assert body["verification_uri"] == "https://verify.test/ABCD-EFGH"
    assert "flow_id" in body


@pytest.mark.asyncio
async def test_start_flow_rejects_non_oauth_provider(oauth_test_client):
    resp = await oauth_test_client.post("/api/oauth/openrouter/start", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_start_flow_unknown_provider_404(oauth_test_client):
    resp = await oauth_test_client.post("/api/oauth/ghost/start", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_unknown_flow_404(oauth_test_client):
    resp = await oauth_test_client.get("/api/oauth/kiro/status/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_pending_then_complete_creates_instance(oauth_test_client):
    c = oauth_test_client
    with patch("routers.oauth.oauth_register_client", AsyncMock(return_value=_REGISTER_RESP)), \
         patch("routers.oauth.oauth_start_device_authorization", AsyncMock(return_value=_AUTHZ_RESP)):
        start_resp = await c.post(
            "/api/oauth/kiro/start",
            json={"name": "mi-cuenta", "profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/p1"},
        )
    flow_id = start_resp.json()["flow_id"]

    pending_resp = httpx.Response(400, text='{"error":"authorization_pending"}')
    with patch("routers.oauth.oauth_poll_token", AsyncMock(return_value=pending_resp)):
        poll_resp = await c.get(f"/api/oauth/kiro/status/{flow_id}")
    assert poll_resp.json()["status"] == "pending"

    complete_resp = httpx.Response(
        200, json={"accessToken": "at1", "refreshToken": "rt1", "expiresIn": 3600}
    )
    with patch("routers.oauth.oauth_poll_token", AsyncMock(return_value=complete_resp)):
        poll_resp2 = await c.get(f"/api/oauth/kiro/status/{flow_id}")

    body = poll_resp2.json()
    assert body["status"] == "complete"
    instance = await db.get_instance(body["instance_id"])
    assert instance is not None
    state = db.get_instance_oauth_state(instance)
    assert state["access_token"] == "at1"
    assert state["profile_arn"] == "arn:aws:codewhisperer:us-east-1:1:profile/p1"
    assert state["status"] == "active"


@pytest.mark.asyncio
async def test_status_complete_without_profile_arn_marks_needs_profile_arn(oauth_test_client):
    """Sin profile_arn informado, una instancia Kiro queda needs_profile_arn (no
    hay forma de resolverlo vía API para cuentas AWS Builder ID)."""
    c = oauth_test_client
    with patch("routers.oauth.oauth_register_client", AsyncMock(return_value=_REGISTER_RESP)), \
         patch("routers.oauth.oauth_start_device_authorization", AsyncMock(return_value=_AUTHZ_RESP)):
        start_resp = await c.post("/api/oauth/kiro/start", json={"name": "mi-cuenta"})
    flow_id = start_resp.json()["flow_id"]

    complete_resp = httpx.Response(
        200, json={"accessToken": "at1", "refreshToken": "rt1", "expiresIn": 3600}
    )
    with patch("routers.oauth.oauth_poll_token", AsyncMock(return_value=complete_resp)):
        poll_resp = await c.get(f"/api/oauth/kiro/status/{flow_id}")

    instance = await db.get_instance(poll_resp.json()["instance_id"])
    state = db.get_instance_oauth_state(instance)
    assert state["status"] == "needs_profile_arn"


@pytest.mark.asyncio
async def test_status_expired_device_code_reports_error(oauth_test_client):
    c = oauth_test_client
    with patch("routers.oauth.oauth_register_client", AsyncMock(return_value=_REGISTER_RESP)), \
         patch("routers.oauth.oauth_start_device_authorization", AsyncMock(return_value=_AUTHZ_RESP)):
        start_resp = await c.post("/api/oauth/kiro/start", json={"name": "mi-cuenta"})
    flow_id = start_resp.json()["flow_id"]

    expired_resp = httpx.Response(400, text='{"error":"expired_token"}')
    with patch("routers.oauth.oauth_poll_token", AsyncMock(return_value=expired_resp)):
        poll_resp = await c.get(f"/api/oauth/kiro/status/{flow_id}")

    body = poll_resp.json()
    assert body["status"] == "error"
    # Ninguna instancia creada tras un flujo fallido.
    assert await db.get_all_instances() == []


@pytest.mark.asyncio
async def test_reauth_replaces_oauth_state_of_existing_instance(oauth_test_client):
    c = oauth_test_client
    await db.upsert_instance({
        "id": "kiro1", "name": "Old", "provider": "kiro", "api_key": "", "is_free": 1,
        "oauth_state": {"status": "needs_reauth"},
    })
    with patch("routers.oauth.oauth_register_client", AsyncMock(return_value={"clientId": "cid2", "clientSecret": "csec2"})), \
         patch("routers.oauth.oauth_start_device_authorization", AsyncMock(return_value={
             "deviceCode": "dc2", "userCode": "WXYZ-1234",
             "verificationUriComplete": "https://verify.test/WXYZ-1234",
             "interval": 5, "expiresIn": 600,
         })):
        start_resp = await c.post(
            "/api/oauth/kiro/reauth/kiro1",
            json={"profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/p1"},
        )
    flow_id = start_resp.json()["flow_id"]

    complete_resp = httpx.Response(
        200, json={"accessToken": "at2", "refreshToken": "rt2", "expiresIn": 3600}
    )
    with patch("routers.oauth.oauth_poll_token", AsyncMock(return_value=complete_resp)):
        poll_resp = await c.get(f"/api/oauth/kiro/status/{flow_id}")

    body = poll_resp.json()
    assert body["status"] == "complete"
    assert body["instance_id"] == "kiro1"
    instance = await db.get_instance("kiro1")
    state = db.get_instance_oauth_state(instance)
    assert state["status"] == "active"
    assert state["access_token"] == "at2"


@pytest.mark.asyncio
async def test_reauth_unknown_instance_404(oauth_test_client):
    resp = await oauth_test_client.post("/api/oauth/kiro/reauth/ghost", json={})
    assert resp.status_code == 404
