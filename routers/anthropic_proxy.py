"""
Proxy Anthropic-compatible — Puerto 8788.
Acepta el formato de Claude Code (POST /v1/messages).
Traduce Anthropic → OpenAI, ejecuta el Router, traduce de vuelta.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.router import router as app_router
from services.translators import (
    anthropic_to_openai,
    openai_stream_to_anthropic,
    openai_to_anthropic,
    resolve_claude_model,
)

router = APIRouter()

MODELS_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "claude-haiku-4-5",   "object": "model", "created": 0, "owned_by": "anthropic"},
        {"id": "claude-sonnet-4-5",  "object": "model", "created": 0, "owned_by": "anthropic"},
        {"id": "claude-opus-4-5",    "object": "model", "created": 0, "owned_by": "anthropic"},
    ],
}


@router.get("/v1/models")
async def list_models():
    return JSONResponse(MODELS_RESPONSE)


@router.post("/v1/messages")
async def messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="Body JSON inválido")

    original_model = body.get("model", "claude-sonnet-4-5")
    tier = resolve_claude_model(original_model)
    is_stream = body.get("stream", False)

    # Traducir a OpenAI con modelo placeholder (el router lo resuelve por model_name)
    openai_body = anthropic_to_openai(body, target_model="placeholder")
    openai_body["stream"] = is_stream

    result = await app_router.acompletion(tier, openai_body, stream=is_stream, proxy_type="anthropic")

    if result.error:
        raise result.error

    deployment_used = result.deployment_used or {}
    extra_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if deployment_used.get("provider"):
        extra_headers["X-Infinity-Provider"] = str(deployment_used["provider"])
    if deployment_used.get("model_id"):
        extra_headers["X-Infinity-Model"] = str(deployment_used["model_id"])

    if is_stream:
        async def stream_gen():
            async for chunk in openai_stream_to_anthropic(result.stream):
                yield chunk.encode() if isinstance(chunk, str) else chunk

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers=extra_headers,
        )
    else:
        anthropic_resp = openai_to_anthropic(result.json)
        return JSONResponse(anthropic_resp, headers=extra_headers)


@router.get("/v1/health")
async def health():
    return {"status": "ok", "proxy": "anthropic"}
