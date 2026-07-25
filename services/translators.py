"""
Traducción Anthropic Messages API ↔ OpenAI Chat Completions.
Portado de infinity-provisioner.py (líneas 3393-3496).
"""

import codecs
import json
from collections.abc import AsyncGenerator

from services.router import UpstreamStreamInterrupted


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """
    Convierte un mensaje Anthropic al formato OpenAI.
    Puede retornar múltiples mensajes (ej: tool_result → role:tool por cada uno).
    """
    role = msg.get("role", "user")
    content = msg.get("content", "")

    # Contenido simple string — sin cambios
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    # Contenido como lista de bloques
    if isinstance(content, list):
        # ── Mensaje de usuario con tool_result ────────────────────────────────
        # Cada tool_result se convierte en un mensaje role:tool separado
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        if tool_results:
            messages = []
            for tr in tool_results:
                result_content = tr.get("content", "")
                # El content puede ser string o lista de bloques de texto
                if isinstance(result_content, list):
                    result_content = " ".join(
                        b.get("text", "") for b in result_content if b.get("type") == "text"
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": result_content or "",
                })
            return messages

        # ── Mensaje de assistant con tool_use ─────────────────────────────────
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = [b for b in content if b.get("type") == "text"]

        if tool_uses:
            tool_calls = []
            for tu in tool_uses:
                tool_calls.append({
                    "id": tu.get("id", f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": tu.get("name", ""),
                        "arguments": json.dumps(tu.get("input", {})),
                    },
                })
            text_content = " ".join(b.get("text", "") for b in texts) if texts else None
            return [{"role": "assistant", "content": text_content, "tool_calls": tool_calls}]

        # ── Bloques de texto puro ─────────────────────────────────────────────
        text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        return [{"role": role, "content": text}]

    return [{"role": role, "content": str(content)}]


def anthropic_to_openai(body: dict, target_model: str) -> dict:
    """
    Convierte body de Anthropic Messages API → OpenAI Chat Completions.
    target_model es el modelo real del proveedor (ej: minimax/minimax-m2.5:free).
    """
    # Convertir cada mensaje del historial
    converted_messages = []
    for msg in body.get("messages", []):
        converted_messages.extend(_convert_anthropic_message(msg))

    oai = {
        "model": target_model,
        "messages": converted_messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
        "temperature": body.get("temperature", 1.0),
    }

    # system en Anthropic va separado; en OpenAI va como primer mensaje
    if "system" in body:
        system_content = body["system"]
        if isinstance(system_content, list):
            system_text = " ".join(
                b.get("text", "") for b in system_content if b.get("type") == "text"
            )
        else:
            system_text = str(system_content)
        oai["messages"] = [{"role": "system", "content": system_text}] + oai["messages"]

    # tools: Anthropic tools → OpenAI tools
    if "tools" in body:
        oai["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in body["tools"]
        ]

    return oai


def _map_stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """Mapea finish_reason (OpenAI) → stop_reason (Anthropic).

    Prioridad: 'length' siempre gana (incluso con tool_calls truncadas a medias),
    replicando el comportamiento documentado de la API real de Anthropic.
    """
    if finish_reason == "length":
        return "max_tokens"
    if has_tool_calls:
        return "tool_use"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


def openai_to_anthropic(data: dict) -> dict:
    """Convierte respuesta OpenAI Chat Completions → Anthropic Messages API (no-stream)."""
    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason")
    tool_calls = message.get("tool_calls") or []

    if tool_calls:
        # Traducir tool_calls al formato Anthropic tool_use
        content = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", f"tool_{len(content)}"),
                "name": fn.get("name", ""),
                "input": args,
            })
    else:
        text = message.get("content", "") or ""
        content = [{"type": "text", "text": text}]

    stop_reason = _map_stop_reason(finish_reason, bool(tool_calls))

    return {
        "type": "message",
        "id": data.get("id", "msg_001"),
        "model": data.get("model", ""),
        "role": "assistant",
        "stop_reason": stop_reason,
        "content": content,
        "usage": {
            "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
        },
    }


async def _iter_sse_lines(byte_stream) -> AsyncGenerator[str, None]:
    """Bufferiza un stream de bytes crudo y produce líneas SSE completas (`data: ...`)."""
    buffer = ""
    # Decoder incremental para no partir caracteres multibyte entre chunks
    # (aiter_bytes no alinea a límites UTF-8 → insertaba � en emojis/acentos/CJK).
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    async for chunk in byte_stream:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")


def _close_open_blocks(text_block_open: bool, tool_blocks: dict) -> list[str]:
    """Genera los `content_block_stop` para el bloque de texto (si abierto) y cada tool block abierto."""
    events = []
    if text_block_open:
        events.append("event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n")
    for tc_idx in tool_blocks:
        events.append(f"event: content_block_stop\ndata: {{\"type\":\"content_block_stop\",\"index\":{tc_idx + 1}}}\n\n")
    return events


async def openai_stream_to_anthropic(byte_stream) -> AsyncGenerator[str, None]:
    """
    Convierte streaming OpenAI SSE (bytes crudos, p.ej. resp.aiter_bytes()) → Anthropic SSE.
    Soporta texto, tool_calls (function calling) y usage (input/output tokens) en vivo.
    """
    msg_start = {
        "type": "message_start",
        "message": {
            "type": "message", "role": "assistant", "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(msg_start)}\n\n"
    yield "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
    yield "event: ping\ndata: {\"type\":\"ping\"}\n\n"

    text_block_open = True   # El bloque de texto (index 0) ya está abierto
    tool_blocks: dict[int, bool] = {}  # tool_call_index → ya se emitió content_block_start
    last_seen_input_tokens: int = 0
    last_seen_output_tokens: int = 0
    stop_reason: str | None = None   # se fija al ver finish_reason
    blocks_closed = False            # content_block_stop ya emitidos

    interrupted = False
    try:
        async for line in _iter_sse_lines(byte_stream):
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if not raw:
                continue

            if raw == "[DONE]":
                break

            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Capturar tokens de uso en cualquier chunk que los incluya.
            # OpenAI (con stream_options.include_usage) manda el usage en un chunk
            # final con choices=[]; hay que leerlo ANTES del `continue` de abajo.
            chunk_usage = chunk.get("usage") or {}
            if chunk_usage.get("prompt_tokens"):
                last_seen_input_tokens = chunk_usage["prompt_tokens"]
            if chunk_usage.get("completion_tokens"):
                last_seen_output_tokens = chunk_usage["completion_tokens"]

            choices = chunk.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # ── Texto normal ──────────────────────────────────────────────────────
            text = delta.get("content", "")
            if text:
                event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
                yield f"event: content_block_delta\ndata: {json.dumps(event)}\n\n"

            # ── Tool calls ────────────────────────────────────────────────────────
            for i, tc in enumerate(delta.get("tool_calls") or []):
                # Gemini omite 'index' en tool_calls; usar la posición como fallback
                # evita que varias tool calls colapsen en el mismo bloque (index 0).
                tc_index = tc.get("index", i)
                anthropic_block_index = tc_index + 1  # texto es index 0, tools empiezan en 1

                if tc_index not in tool_blocks:
                    # Primer chunk de este tool — cerrar texto y abrir bloque tool_use
                    if text_block_open:
                        yield "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
                        text_block_open = False

                    fn = tc.get("function", {})
                    start_event = {
                        "type": "content_block_start",
                        "index": anthropic_block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id", f"tool_{tc_index}"),
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(start_event)}\n\n"
                    tool_blocks[tc_index] = True

                # Emitir fragmento de argumentos JSON
                args_fragment = (tc.get("function") or {}).get("arguments", "")
                if args_fragment:
                    delta_event = {
                        "type": "content_block_delta",
                        "index": anthropic_block_index,
                        "delta": {"type": "input_json_delta", "partial_json": args_fragment},
                    }
                    yield f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n"

            # ── Fin del stream ────────────────────────────────────────────────────
            # Al ver finish_reason solo cerramos los bloques de contenido. El
            # message_delta final (con los tokens) se difiere al final del stream,
            # porque con include_usage el usage llega en un chunk POSTERIOR.
            if finish and not blocks_closed:
                stop_reason = _map_stop_reason(finish, bool(tool_blocks))
                for event_str in _close_open_blocks(text_block_open, tool_blocks):
                    yield event_str
                text_block_open = False
                blocks_closed = True
    except UpstreamStreamInterrupted:
        interrupted = True

    if interrupted:
        for event_str in _close_open_blocks(text_block_open, tool_blocks):
            yield event_str
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": "upstream stream interrupted"},
        }
        yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
        return

    # ── Finalización (tras [DONE] o fin del stream) ───────────────────────────
    if not blocks_closed:
        for event_str in _close_open_blocks(text_block_open, tool_blocks):
            yield event_str

    stop_event = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason or "end_turn", "stop_sequence": None},
        "usage": {
            "input_tokens": last_seen_input_tokens,
            "output_tokens": last_seen_output_tokens,
        },
    }
    yield f"event: message_delta\ndata: {json.dumps(stop_event)}\n\n"
    yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"


def resolve_claude_model(model: str) -> str:
    """Mapea nombres de modelos Claude a los tiers de infinity."""
    model_lower = model.lower()
    if "opus" in model_lower:
        return "opus"
    if "haiku" in model_lower:
        return "haiku"
    return "sonnet"
