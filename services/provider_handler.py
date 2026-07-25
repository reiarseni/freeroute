"""
ProviderHandler — abstraction for provider-specific HTTP prep and error classification.

Cada proveedor (openrouter, gemini, groq, zai, zen, kilo) implementa:
  - pre_call(deployment, body) -> PreparedCall(url, headers, body)
  - parse_error(status_code, body) -> ErrorClassification

El handler registry es la fuente única de verdad para URLs y headers:
ningún otro módulo (api_keys.py, provider_models.py, etc.) debe hardcodear URLs.

Refactor del v4 (donde _build_headers, PROVIDER_BASES, PROVIDER_VALIDATE_URLS
estaban duplicados en 4 archivos).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

import httpx

# ── Tipos ────────────────────────────────────────────────────────────────────


class ErrorType(str, Enum):
    """Categorías canónicas de error para cooldown y fallback tipado."""
    RATE_LIMIT = "RATE_LIMIT"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    SERVER_ERROR = "SERVER_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    TIMEOUT = "TIMEOUT"
    CONTENT_POLICY_VIOLATION = "CONTENT_POLICY_VIOLATION"
    CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ErrorClassification:
    """Resultado de parse_error: tipo + info cruda para logging."""
    error_type: ErrorType
    raw_status: int | None = None
    raw_body: str | None = None


@dataclass
class PreparedCall:
    """Salida de ProviderHandler.pre_call: lo mínimo para disparar la request."""
    url: str
    headers: dict[str, str]
    body: dict


class ProviderHandler(Protocol):
    """Protocolo que todo handler de proveedor implementa.

    pre_call es async (BREAKING para implementadores externos — ninguno existe
    hoy fuera de este repo) para poder refrescar el access_token de instancias
    oauth_device justo antes de construir la request.
    """
    name: str
    base_url: str
    models_url: str

    async def pre_call(self, deployment: dict, api_instance: dict, body: dict) -> PreparedCall: ...

    def parse_error(self, status_code: int, body: str) -> ErrorClassification: ...

    async def translate_request(self, body: dict) -> dict: ...

    def translate_stream(self, raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]: ...


KIRO_MAX_PAYLOAD_BYTES = 615_000  # límite empírico documentado en design.md; sin auto-trim

_AWS_REGION_RE = re.compile(r"\b([a-z]{2}(?:-gov)?-[a-z]+-\d)\b")


def _extract_aws_region(raw: str, default: str = "us-east-1") -> str:
    """Extrae el código de región AWS (p.ej. "us-east-1") de una URL o string libre
    (p.ej. `oauth_state.region` almacena hoy la base_url del OIDC endpoint)."""
    m = _AWS_REGION_RE.search(raw or "")
    return m.group(1) if m else default


# ── Helpers ≈válidos para todos los handlers ─────────────────────────────────


def _parse_generic_error(status_code: int, body: str) -> ErrorClassification:
    """Mapeo estándar status_code → ErrorType que casi todos los providers cumplen.

    STATUS → ERROR:
      400 → si el body indica contexto excedido → CONTEXT_WINDOW_EXCEEDED
           si el body indica content policy → CONTENT_POLICY_VIOLATION
           sino UNKNOWN
      401, 403 → AUTH_ERROR
      404 → MODEL_NOT_FOUND
      429 → RATE_LIMIT
      5xx → SERVER_ERROR
      otro → UNKNOWN

    Excepción por CUERPO (antes que el status): algunos providers devuelven errores
    de saturación/concurrencia transitorios ("ResourceExhausted: Worker local total
    request limit reached (33/32)", "overloaded"…) con status 400/500 en vez de 429.
    Se clasifican como RATE_LIMIT para que sean retryables y disparen fallback en
    lugar de quedar como UNKNOWN (que no reintenta ni cooldownea).
    """
    body_lower = (body or "").lower()
    if any(s in body_lower for s in (
        "resourceexhausted", "resource exhausted",
        "request limit reached", "worker local",
        "overloaded", "too many concurrent", "concurrent request",
    )):
        return ErrorClassification(ErrorType.RATE_LIMIT, status_code, body)

    if status_code == 429:
        return ErrorClassification(ErrorType.RATE_LIMIT, status_code, body)
    if status_code == 404:
        return ErrorClassification(ErrorType.MODEL_NOT_FOUND, status_code, body)
    if status_code in (401, 403):
        return ErrorClassification(ErrorType.AUTH_ERROR, status_code, body)
    if status_code >= 500:
        return ErrorClassification(ErrorType.SERVER_ERROR, status_code, body)

    if status_code == 400:
        body_lower = (body or "").lower()
        if "context" in body_lower and ("length" in body_lower or "window" in body_lower
                                         or "exceed" in body_lower or "too long" in body_lower):
            return ErrorClassification(ErrorType.CONTEXT_WINDOW_EXCEEDED, status_code, body)
        if "content policy" in body_lower or "content_filter" in body_lower or "content filter" in body_lower:
            return ErrorClassification(ErrorType.CONTENT_POLICY_VIOLATION, status_code, body)
        return ErrorClassification(ErrorType.UNKNOWN, status_code, body)

    return ErrorClassification(ErrorType.UNKNOWN, status_code, body)


# ── Handler data-driven ───────────────────────────────────────────────────────


class DataDrivenHandler:
    """Handler construido desde la config de un provider en DB (tabla `providers`).

    Reemplaza las antiguas clases hardcodeadas (OpenRouterHandler, ZenHandler...).
    Toda la variación por provider (base_url, models_url, auth, headers) ahora es data.
    """

    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.base_url: str = (cfg.get("base_url") or "").rstrip("/")
        self.models_url: str = cfg.get("models_url") or ""
        self.auth_type: str = cfg.get("auth_type") or "bearer"
        self.auth_value: str = cfg.get("auth_value") or ""
        self.extra_headers: dict = cfg.get("extra_headers") or {}
        self.kind: str = cfg.get("kind") or "plain"
        # True si el upstream de este provider responde siempre en su propio
        # formato streaming (p.ej. Kiro: `generateAssistantResponse` es AWS event
        # stream incluso cuando el cliente pide `stream: false`). El router usa
        # esto para reducir translate_stream a un único JSON en vez de leer
        # resp.json() directamente.
        self.response_is_stream: bool = self.kind == "kiro"

    async def _headers(self, api_instance: dict | None) -> dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        if self.auth_type == "bearer":
            key = (api_instance or {}).get("api_key", "")
            if key:
                h["Authorization"] = f"Bearer {key}"
        elif self.auth_type == "static":
            if self.auth_value:
                h["Authorization"] = self.auth_value
        elif self.auth_type == "oauth_device":
            # El refresh perezoso (si hacía falta) ya ocurrió antes de llegar aquí
            # (ver Router._ensure_oauth_fresh) — solo inyectamos el token vigente.
            state = parse_oauth_state(api_instance or {})
            token = state.get("access_token", "")
            if token:
                h["Authorization"] = f"Bearer {token}"
        # keyless: sin Authorization (solo User-Agent)
        h.update(self.extra_headers)
        return h

    async def pre_call(self, deployment: dict, api_instance: dict | None, body: dict) -> PreparedCall:
        if self.kind == "kiro":
            return await self._pre_call_kiro(deployment, api_instance, body)
        url = f"{self.base_url}/chat/completions"
        new_body = {**body, "model": deployment["model_id"]}
        return PreparedCall(url, await self._headers(api_instance), new_body)

    async def _pre_call_kiro(self, deployment: dict, api_instance: dict | None, body: dict) -> PreparedCall:
        # Construye la request real contra la API de Kiro (AWS CodeWhisperer). El
        # body OpenAI-compatible se traduce aquí (no en translate_request) porque
        # necesita `deployment["model_id"]`, que translate_request no recibe.
        from services.kiro_translator import build_kiro_payload

        state = parse_oauth_state(api_instance or {})
        token = state.get("access_token", "")
        profile_arn = state.get("profile_arn", "")
        region = _extract_aws_region(state.get("region", ""))

        kiro_body = build_kiro_payload(body, model_id=deployment["model_id"], profile_arn=profile_arn)
        payload_size = len(json.dumps(kiro_body).encode())
        if payload_size > KIRO_MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"Payload Kiro de {payload_size} bytes excede el límite de "
                f"{KIRO_MAX_PAYLOAD_BYTES} bytes (~615KB); recorta el historial de la conversación."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-amz-json-1.0",
            "x-amz-target": "AmazonCodeWhispererStreamingService.GenerateAssistantResponse",
            "User-Agent": (
                "aws-sdk-js/1.0.27 ua/2.1 os/linux lang/js md/nodejs#22 "
                "api/codewhispererstreaming#1.0.27 m/E KiroIDE-0.7.45-infinity-provisioner"
            ),
            "x-amz-user-agent": "aws-sdk-js/1.0.27 KiroIDE-0.7.45-infinity-provisioner",
            "x-amzn-codewhisperer-optout": "true",
            "x-amzn-kiro-agent-mode": "vibe",
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=3",
        }
        return PreparedCall(f"https://runtime.{region}.kiro.dev/", headers, kiro_body)

    def parse_error(self, status_code: int, body: str) -> ErrorClassification:
        return _parse_generic_error(status_code, body)

    async def translate_request(self, body: dict) -> dict:
        """Hook opcional: por defecto, paso-a-través. `kind=kiro` traduce el body
        dentro de `pre_call` en vez de aquí (ver `_pre_call_kiro`)."""
        return body

    async def translate_stream(self, raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Hook opcional: por defecto, paso-a-través. `kind=kiro` traduce el event
        stream binario de Kiro a chunks SSE OpenAI-compatible."""
        if self.kind == "kiro":
            from services.kiro_translator import kiro_events_to_openai_sse
            async for chunk in kiro_events_to_openai_sse(raw):
                yield chunk
            return
        async for chunk in raw:
            yield chunk


# ── Registry runtime (poblado desde DB por el router en cada reload) ───────────

_RUNTIME_HANDLERS: dict[str, DataDrivenHandler] = {}
KNOWN_PROVIDERS: set[str] = set()


def set_runtime_providers(cfgs: list[dict]) -> None:
    """Reemplaza el registry runtime con las configs de providers venidas de DB.
    Lo llama el router en _reload_cache. Actualiza también KNOWN_PROVIDERS."""
    global _RUNTIME_HANDLERS, KNOWN_PROVIDERS
    _RUNTIME_HANDLERS = {c["name"]: DataDrivenHandler(c) for c in cfgs}
    KNOWN_PROVIDERS = set(_RUNTIME_HANDLERS.keys())


def build_handler(cfg: dict) -> DataDrivenHandler:
    """Construye un handler suelto desde una config (sin tocar el registry).
    Útil para api_keys.py / provider_models.py que arman un handler puntual."""
    return DataDrivenHandler(cfg)


def get_handler(provider_name: str) -> ProviderHandler:
    """Devuelve el handler runtime para el nombre dado. KeyError si no existe."""
    return _RUNTIME_HANDLERS[provider_name]


# ── OAuth 2.0 Device Authorization Grant (RFC 8628) ─────────────────────────
#
# Genérico por auth_type=oauth_device; las rutas concretas varían por `kind`
# (hoy solo "kiro" = AWS SSO OIDC). Si un futuro provider habla RFC 8628 con
# rutas distintas, se añade una entrada nueva aquí sin tocar el auth_type.

OAUTH_DEVICE_PATHS: dict[str, dict[str, str]] = {
    "kiro": {
        "register": "/client/register",
        "device_authorization": "/device_authorization",
        "token": "/token",
    },
}
_DEFAULT_OAUTH_PATHS = OAUTH_DEVICE_PATHS["kiro"]

# AWS SSO OIDC exige `startUrl` en StartDeviceAuthorization (RFC 8628 + extensión AWS).
# Kiro se autentica contra AWS Builder ID, cuyo start_url público es fijo.
OAUTH_START_URLS: dict[str, str] = {
    "kiro": "https://view.awsapps.com/start",
}


class OAuthReauthRequired(Exception):
    """El refresh_token fue rechazado (revocado/expirado) por el proveedor OIDC."""


def oauth_paths(kind: str) -> dict[str, str]:
    return OAUTH_DEVICE_PATHS.get(kind, _DEFAULT_OAUTH_PATHS)


def parse_oauth_state(instance: dict) -> dict:
    """Deserializa api_instances.oauth_state (TEXT JSON)."""
    try:
        return json.loads(instance.get("oauth_state") or "{}")
    except (ValueError, TypeError, AttributeError):
        return {}


def oauth_expires_at(token_resp: dict) -> str:
    """ISO8601 UTC a partir de expiresIn/expires_in (default 3600s si ausente)."""
    expires_in = token_resp.get("expiresIn", token_resp.get("expires_in", 3600))
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 3600
    return (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()


def oauth_needs_refresh(state: dict, margin_seconds: float = 60.0) -> bool:
    """True si el access_token expira dentro de `margin_seconds` o ya expiró."""
    expires_at = state.get("expires_at")
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    return (exp - datetime.now(timezone.utc)).total_seconds() <= margin_seconds


# Scopes CodeWhisperer requeridos para que el access_token resultante pueda usarse
# en `generateAssistantResponse` — sin ellos, AWS SSO OIDC emite un token válido
# para refrescar pero sin permisos de chat (`403 "bearer token invalid"`, causa
# raíz confirmada empíricamente y documentada en design.md Decisión 4bis).
OAUTH_SCOPES: dict[str, list[str]] = {
    "kiro": ["codewhisperer:completions", "codewhisperer:analysis"],
}


async def oauth_register_client(
    client: httpx.AsyncClient, base_url: str, kind: str, client_name: str,
) -> dict:
    paths = oauth_paths(kind)
    body: dict = {"clientName": client_name, "clientType": "public"}
    scopes = OAUTH_SCOPES.get(kind)
    if scopes:
        body["scopes"] = scopes
    resp = await client.post(
        f"{base_url}{paths['register']}",
        json=body,
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


async def oauth_start_device_authorization(
    client: httpx.AsyncClient, base_url: str, kind: str, client_id: str, client_secret: str,
) -> dict:
    paths = oauth_paths(kind)
    resp = await client.post(
        f"{base_url}{paths['device_authorization']}",
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "startUrl": OAUTH_START_URLS.get(kind, OAUTH_START_URLS["kiro"]),
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


async def oauth_poll_token(
    client: httpx.AsyncClient, base_url: str, kind: str,
    client_id: str, client_secret: str, device_code: str,
) -> httpx.Response:
    """Un intento de polling contra /token (grantType=device_code).

    Devuelve la respuesta cruda (200 → éxito; el caller inspecciona el body en
    4xx para distinguir authorization_pending / slow_down / expired_token)."""
    paths = oauth_paths(kind)
    return await client.post(
        f"{base_url}{paths['token']}",
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "grantType": "urn:ietf:params:oauth:grant-type:device_code",
            "deviceCode": device_code,
        },
        timeout=10.0,
    )


async def oauth_refresh_token(
    client: httpx.AsyncClient, base_url: str, kind: str,
    client_id: str, client_secret: str, refresh_token: str,
) -> dict:
    """Refresca el access_token. Lanza OAuthReauthRequired si el refresh_token
    fue rechazado (revocado/expirado) por el proveedor OIDC."""
    paths = oauth_paths(kind)
    resp = await client.post(
        f"{base_url}{paths['token']}",
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "grantType": "refresh_token",
            "refreshToken": refresh_token,
        },
        timeout=10.0,
    )
    if resp.status_code >= 400:
        if resp.status_code in (400, 401, 403):
            raise OAuthReauthRequired(resp.text)
        resp.raise_for_status()
    return resp.json()


def parse_model_id_prefix(model_id: str) -> tuple[str | None, str]:
    """Detecta prefijo de provider en model_id.

    Si el prefijo (antes de la primera /) corresponde a un provider conocido,
    devuelve (handler_name, actual_model). Si no, devuelve (None, model_id).

    Ejemplos:
      "groq/openai/gpt-oss-120b"        → ("groq", "openai/gpt-oss-120b")
      "minimax/minimax-m2.5:free"       → (None, "minimax/minimax-m2.5:free")
                                         # "minimax" no es provider conocido
    """
    if "/" not in model_id:
        return (None, model_id)
    prefix, rest = model_id.split("/", 1)
    if prefix in KNOWN_PROVIDERS:
        return (prefix, rest)
    return (None, model_id)
