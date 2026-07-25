"""
REST API — OAuth 2.0 Device Authorization Grant (RFC 8628) para providers
auth_type=oauth_device (hoy: Kiro / AWS SSO OIDC).

El estado de un flujo EN CURSO (client_id/secret + device_code) vive en
memoria de proceso con TTL corto — es efímero, no se persiste en DB. Si el
servidor reinicia a mitad de un flujo, el usuario simplemente reintenta; el
device_code ya habría expirado en el proveedor de todos modos.
"""

import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from services.provider_handler import (
    oauth_expires_at,
    oauth_poll_token,
    oauth_register_client,
    oauth_start_device_authorization,
)
from services.router import router as app_router

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

_FLOW_TTL_S = 15 * 60  # techo generoso frente al TTL real del device_code
_flows: dict[str, dict] = {}


def _cleanup_flows() -> None:
    now = time.monotonic()
    for fid in [f for f, v in _flows.items() if v["deadline"] < now]:
        _flows.pop(fid, None)


class StartIn(BaseModel):
    name: str = ""
    profile_arn: str = ""


async def _get_oauth_provider(provider: str) -> dict:
    cfg = await db.get_provider(provider)
    if not cfg:
        raise HTTPException(404, detail=f"Provider '{provider}' no encontrado")
    if cfg.get("auth_type") != "oauth_device":
        raise HTTPException(422, detail=f"Provider '{provider}' no usa oauth_device")
    return cfg


@router.post("/{provider}/start")
async def start_flow(provider: str, data: StartIn):
    _cleanup_flows()
    cfg = await _get_oauth_provider(provider)

    await app_router._ensure_init()
    client = app_router._client

    try:
        reg = await oauth_register_client(client, cfg["base_url"], cfg["kind"], "infinity-provisioner")
        client_id = reg.get("clientId")
        client_secret = reg.get("clientSecret")
        auth = await oauth_start_device_authorization(
            client, cfg["base_url"], cfg["kind"], client_id, client_secret
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"Error iniciando device flow: {exc}") from exc

    interval = auth.get("interval", 5)
    expires_in = auth.get("expiresIn", 600)
    flow_id = uuid.uuid4().hex
    _flows[flow_id] = {
        "provider": provider,
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": auth.get("deviceCode"),
        "reauth_instance_id": None,
        "name": data.name,
        "profile_arn": data.profile_arn.strip(),
        "deadline": time.monotonic() + min(_FLOW_TTL_S, expires_in + 60),
        "expires_at_wall": time.monotonic() + expires_in,
        "status": "pending",
    }
    return {
        "flow_id": flow_id,
        "user_code": auth.get("userCode"),
        "verification_uri": auth.get("verificationUriComplete") or auth.get("verificationUri"),
        "interval": interval,
    }


@router.get("/{provider}/status/{flow_id}")
async def poll_status(provider: str, flow_id: str):
    flow = _flows.get(flow_id)
    if not flow or flow["provider"] != provider:
        raise HTTPException(404, detail="Flujo no encontrado o expirado")

    if flow["status"] in ("complete", "error"):
        return {"status": flow["status"], "message": flow.get("message", ""),
                "instance_id": flow.get("instance_id")}

    if time.monotonic() > flow["expires_at_wall"]:
        flow["status"] = "error"
        flow["message"] = "El código de dispositivo expiró antes de ser autorizado"
        return {"status": "error", "message": flow["message"]}

    cfg = await _get_oauth_provider(provider)
    await app_router._ensure_init()
    client = app_router._client

    resp = await oauth_poll_token(
        client, cfg["base_url"], cfg["kind"], flow["client_id"], flow["client_secret"], flow["device_code"],
    )

    if resp.status_code == 200:
        token = resp.json()
        profile_arn = flow.get("profile_arn", "")
        # Kiro exige profileArn en toda request de chat, y no hay forma de
        # resolverlo vía API para cuentas AWS Builder ID (ver design.md Decisión 4
        # revisada) — sin él, la instancia queda marcada needs_profile_arn y
        # excluida del enrutado hasta que el usuario reautentique aportándolo.
        needs_profile_arn = cfg.get("kind") == "kiro" and not profile_arn
        oauth_state = {
            "client_id": flow["client_id"],
            "client_secret": flow["client_secret"],
            "refresh_token": token.get("refreshToken", token.get("refresh_token", "")),
            "access_token": token.get("accessToken", token.get("access_token", "")),
            "expires_at": oauth_expires_at(token),
            "region": cfg.get("base_url", ""),
            "profile_arn": profile_arn,
            "status": "needs_profile_arn" if needs_profile_arn else "active",
        }
        instance_id = flow["reauth_instance_id"]
        if instance_id:
            existing = await db.get_instance(instance_id)
            await db.upsert_instance({
                "id": instance_id,
                "name": existing["name"] if existing else instance_id,
                "provider": provider,
                "api_key": "",
                "is_free": existing["is_free"] if existing else 1,
                "oauth_state": oauth_state,
            })
        else:
            instance_id = f"{provider}-{uuid.uuid4().hex[:8]}"
            await db.upsert_instance({
                "id": instance_id,
                "name": flow["name"] or f"{cfg.get('label') or provider} ({instance_id})",
                "provider": provider,
                "api_key": "",
                "is_free": 1,
                "oauth_state": oauth_state,
            })
        app_router.invalidate_cache()
        flow["status"] = "complete"
        flow["instance_id"] = instance_id
        return {"status": "complete", "instance_id": instance_id}

    body_lower = resp.text.lower()
    if "authorization_pending" in body_lower or "slow_down" in body_lower:
        return {"status": "pending"}
    if "expired_token" in body_lower:
        flow["status"] = "error"
        flow["message"] = "El código de dispositivo expiró"
        return {"status": "error", "message": flow["message"]}

    flow["status"] = "error"
    flow["message"] = f"Error de autorización: {resp.text}"
    return {"status": "error", "message": flow["message"]}


@router.post("/{provider}/reauth/{instance_id}")
async def reauth_flow(provider: str, instance_id: str, data: StartIn):
    _cleanup_flows()
    existing = await db.get_instance(instance_id)
    if not existing:
        raise HTTPException(404, detail=f"Instancia '{instance_id}' no encontrada")
    result = await start_flow(provider, data)
    _flows[result["flow_id"]]["reauth_instance_id"] = instance_id
    return result
