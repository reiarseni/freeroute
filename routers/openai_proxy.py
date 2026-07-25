"""
Proxy OpenAI-compatible — Puerto 8787.
Acepta POST /v1/chat/completions con model="<cualquier model_name>".
Usa el Router LiteLLM-style en vez de chain_router.
"""

import codecs
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import db
from services.router import UpstreamStreamInterrupted
from services.router import router as app_router

router = APIRouter()


@router.get("/v1/models")
async def list_models():
    model_names_data = await db.get_distinct_model_names()
    model_names = [d["model_name"] if isinstance(d, dict) else d for d in model_names_data]
    if not model_names:
        # Fallback si no hay deployments configurados
        model_names = ["infinity/haiku", "infinity/sonnet", "infinity/opus"]
    data = [
        {"id": name, "object": "model", "created": 0, "owned_by": "infinity"}
        for name in model_names
    ]
    return JSONResponse({"object": "list", "data": data})


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="Body JSON inválido")

    model = body.get("model", "")
    if not model:
        raise HTTPException(400, detail="Campo 'model' requerido")

    is_stream = body.get("stream", False)

    result = await app_router.acompletion(model, body, stream=is_stream, proxy_type="openai")

    if result.error:
        raise result.error

    # Metadata de depuración del deployment realmente usado (igual patrón que
    # el proxy Anthropic): aditivo, no cambia el body de la respuesta, así que
    # clientes que ignoran headers desconocidos no se ven afectados.
    deployment_used = result.deployment_used or {}
    extra_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if deployment_used.get("provider"):
        extra_headers["X-FreeRoute-Provider"] = str(deployment_used["provider"])
    if deployment_used.get("model_id"):
        extra_headers["X-FreeRoute-Model"] = str(deployment_used["model_id"])

    if is_stream:
        async def stream_gen():
            buffer = ""
            # Decoder incremental: mantiene los bytes de un carácter multibyte
            # partido entre chunks hasta completarlo, evitando insertar � (el
            # split arbitrario de aiter_bytes rompía emojis/acentos/CJK).
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            # Un cierre normal SIEMPRE termina en `data: [DONE]`, lo emita el
            # upstream o no: sin terminador el cliente ve la conexión HTTP cerrarse
            # a mitad y lo reporta como "Streaming response failed" aunque la
            # respuesta esté completa. `done_sent` evita duplicarlo si ya vino.
            done_sent = False

            def _is_done(line: str) -> bool:
                return line.startswith("data:") and line[5:].strip() == "[DONE]"

            def _render(line: str) -> str:
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        return f"data: {_patch_tool_calls_index(payload)}\n\n"
                return f"{line}\n\n"

            try:
                async for chunk in result.stream:
                    buffer += decoder.decode(chunk)
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        done_sent = done_sent or _is_done(line)
                        yield _render(line)

                # Flush del residuo: muchos upstream cierran el stream sin salto
                # final, así que la última línea (a menudo el propio [DONE], o el
                # chunk con finish_reason) se quedaba en el buffer y se perdía.
                buffer += decoder.decode(b"", final=True)
                line = buffer.strip()
                if line:
                    done_sent = done_sent or _is_done(line)
                    yield _render(line)

                # Solo el cierre NORMAL se termina con [DONE]. En los caminos de
                # error se omite a propósito: un stream sin terminador es la señal
                # de que la respuesta quedó truncada y el cliente debe decirlo.
                if not done_sent:
                    yield "data: [DONE]\n\n"
            except UpstreamStreamInterrupted:
                error_chunk = {
                    "error": {"message": "upstream stream interrupted", "type": "upstream_error", "code": None}
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
            except Exception as exc:
                # Cualquier otro fallo al relayar: se informa dentro del stream en
                # vez de reventar la respuesta HTTP a medias (que el cliente sólo
                # puede interpretar como stream truncado).
                error_chunk = {
                    "error": {"message": f"proxy stream error: {exc}", "type": "proxy_error", "code": None}
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers=extra_headers,
        )
    else:
        return JSONResponse(result.json, headers=extra_headers)


def _patch_tool_calls_index(payload: str) -> str:
    """
    Gemini omite el campo 'index' en los items de tool_calls dentro de los chunks SSE.
    OpenCode (y otros clientes) lo requieren estrictamente como número.
    Este patch lo inyecta si falta.
    """
    try:
        chunk = json.loads(payload)
        for choice in chunk.get("choices", []):
            tool_calls = choice.get("delta", {}).get("tool_calls")
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    if "index" not in tc:
                        tc["index"] = i
        return json.dumps(chunk, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        return payload
