"""
kiro_translator — traducción de conversación OpenAI-compatible <-> conversationState de Kiro
(AWS CodeWhisperer/Q Developer), parseo del stream de respuesta y lista estática de modelos.

Reglas de conversación portadas de `kiro-gateway/kiro/converters_core.py` (jwadow/kiro-gateway,
AGPL-3.0), adaptadas al body OpenAI-compatible que ya usa el router en vez de su abstracción
`UnifiedMessage` (multi-API). Sin soporte de imágenes ni thinking mode (fuera de alcance de esta
primera versión, ver design.md Non-Goals).

El parser de event stream es heurístico (no framing binario formal), portado de
`kiro-gateway/kiro/parsers.py::AwsEventStreamParser` — ver design.md Decisión 2 revisada.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

MAX_TOOL_NAME_LEN = 64
EMPTY_PLACEHOLDER = "(empty placeholder)"

# Lista estática de model IDs conocidos y confirmados de Kiro (ver proposal.md).
# El listado dinámico real (ListAvailableModels) queda fuera de alcance.
KIRO_STATIC_MODELS: list[str] = [
    "auto",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "claude-haiku-4.5",
    "claude-opus-4.5",
    "claude-opus-4.6",
    "claude-opus-4.7",
    "deepseek-3.2",
    "glm-5",
    "minimax-m2.1",
    "minimax-m2.5",
    "qwen3-coder-next",
]


def _extract_text(content: Any) -> str:
    """Extrae texto plano de un `content` OpenAI (string o lista de bloques)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in ("text", None):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _messages_to_turns(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convierte mensajes OpenAI-compatible a turnos user/assistant intermedios.

    Devuelve (system_prompt, turns). Cada turn: {"role": "user"|"assistant",
    "content": str, "tool_calls": [...], "tool_results": [...]}. Los mensajes
    `role=tool` se convierten en un turno "user" con `tool_results` (Kiro exige que
    los resultados de tool viajen dentro de un turno user, igual que Anthropic).
    """
    system_parts: list[str] = []
    turns: list[dict] = []

    for m in messages:
        role = m.get("role")
        if role == "system" or role == "developer":
            text = _extract_text(m.get("content"))
            if text:
                system_parts.append(text)
        elif role == "user":
            turns.append({
                "role": "user", "content": _extract_text(m.get("content")),
                "tool_calls": [], "tool_results": [],
            })
        elif role == "assistant":
            tool_calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                name = (fn.get("name") or "")[:MAX_TOOL_NAME_LEN]
                try:
                    tool_input = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    tool_input = {}
                tool_calls.append({
                    "toolUseId": tc.get("id", ""), "name": name, "input": tool_input,
                })
            turns.append({
                "role": "assistant", "content": _extract_text(m.get("content")),
                "tool_calls": tool_calls, "tool_results": [],
            })
        elif role == "tool":
            content = _extract_text(m.get("content"))
            turns.append({
                "role": "user", "content": "", "tool_calls": [],
                "tool_results": [{
                    "toolUseId": m.get("tool_call_id", ""),
                    "content": [{"text": content}],
                    "status": "error" if m.get("is_error") else "success",
                }],
            })
        # roles desconocidos se ignoran (Kiro solo soporta user/assistant)

    return "\n\n".join(system_parts), turns


def _merge_adjacent(turns: list[dict]) -> list[dict]:
    """Fusiona turnos consecutivos del mismo rol (p.ej. varios `tool` seguidos)."""
    if not turns:
        return turns
    merged = [dict(turns[0])]
    for t in turns[1:]:
        last = merged[-1]
        if t["role"] == last["role"]:
            if t["content"]:
                last["content"] = f"{last['content']}\n{t['content']}" if last["content"] else t["content"]
            last["tool_calls"] += t["tool_calls"]
            last["tool_results"] += t["tool_results"]
        else:
            merged.append(dict(t))
    return merged


def _strip_tool_content(turns: list[dict]) -> list[dict]:
    """Kiro rechaza `toolResults` sin `tools` declarados: limpia tool_calls/tool_results
    de todos los turnos y descarta los turnos user que quedan sin contenido propio."""
    cleaned = []
    for t in turns:
        t = dict(t)
        t["tool_calls"] = []
        t["tool_results"] = []
        if t["role"] == "user" and not t["content"]:
            continue
        cleaned.append(t)
    return cleaned


def _ensure_first_is_user(turns: list[dict]) -> list[dict]:
    if turns and turns[0]["role"] != "user":
        return [{"role": "user", "content": EMPTY_PLACEHOLDER, "tool_calls": [], "tool_results": []}] + turns
    return turns


def _ensure_alternating(turns: list[dict]) -> list[dict]:
    if len(turns) < 2:
        return turns
    result = [turns[0]]
    for t in turns[1:]:
        if t["role"] == result[-1]["role"]:
            filler_role = "assistant" if t["role"] == "user" else "user"
            result.append({
                "role": filler_role, "content": EMPTY_PLACEHOLDER,
                "tool_calls": [], "tool_results": [],
            })
        result.append(t)
    return result


def _turn_to_history_entry(t: dict, model_id: str) -> dict:
    content = t["content"] or EMPTY_PLACEHOLDER
    if t["role"] == "user":
        user_input: dict = {"content": content, "modelId": model_id, "origin": "AI_EDITOR"}
        if t["tool_results"]:
            user_input["userInputMessageContext"] = {"toolResults": t["tool_results"]}
        return {"userInputMessage": user_input}
    assistant_response: dict = {"content": content}
    if t["tool_calls"]:
        assistant_response["toolUses"] = t["tool_calls"]
    return {"assistantResponseMessage": assistant_response}


def build_kiro_payload(body: dict, model_id: str, profile_arn: str) -> dict:
    """Construye el payload `conversationState` de Kiro a partir de un body OpenAI-compatible.

    Porta las reglas de `kiro-gateway/kiro/converters_core.py::build_kiro_payload`: primer
    mensaje user, roles alternados, assistant antes de tool_result, placeholder de contenido
    vacío, límite de 64 caracteres en nombres de tool, y limpieza de contenido de tools si el
    body no declara ningún `tools`.
    """
    messages = body.get("messages") or []
    tools = body.get("tools") or []

    system_prompt, turns = _messages_to_turns(messages)

    if not tools:
        turns = _strip_tool_content(turns)

    turns = _merge_adjacent(turns)
    turns = _ensure_first_is_user(turns)
    turns = _ensure_alternating(turns)

    if not turns:
        turns = [{"role": "user", "content": EMPTY_PLACEHOLDER, "tool_calls": [], "tool_results": []}]

    if system_prompt and turns[0]["role"] == "user":
        turns[0] = dict(turns[0])
        turns[0]["content"] = f"{system_prompt}\n\n{turns[0]['content']}" if turns[0]["content"] else system_prompt

    history_turns, current = turns[:-1], turns[-1]

    history = [_turn_to_history_entry(t, model_id) for t in history_turns]

    if current["role"] == "assistant":
        history.append(_turn_to_history_entry(current, model_id))
        current = {"role": "user", "content": EMPTY_PLACEHOLDER, "tool_calls": [], "tool_results": []}

    user_input: dict = {
        "content": current["content"] or EMPTY_PLACEHOLDER,
        "modelId": model_id,
        "origin": "AI_EDITOR",
    }
    context: dict = {}
    if tools:
        context["tools"] = [
            {
                "toolSpecification": {
                    "name": (t.get("function", {}).get("name") or "")[:MAX_TOOL_NAME_LEN],
                    "description": t.get("function", {}).get("description") or "",
                    "inputSchema": {
                        "json": t.get("function", {}).get("parameters")
                        or {"type": "object", "properties": {}}
                    },
                }
            }
            for t in tools if t.get("type") == "function"
        ]
    if current["tool_results"]:
        context["toolResults"] = current["tool_results"]
    if context:
        user_input["userInputMessageContext"] = context

    payload: dict = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {"userInputMessage": user_input},
        },
    }
    if history:
        payload["conversationState"]["history"] = history
    if profile_arn:
        payload["profileArn"] = profile_arn

    return payload


# ── Parser heurístico del stream de respuesta (AWS event stream) ──────────────────────────────
#
# AWS devuelve los eventos en framing binario propio (headers + CRC32). En vez de implementar
# ese framing, se decodifica cada chunk como UTF-8 ignorando bytes no válidos (el framing binario
# se descarta silenciosamente) y se localizan los fragmentos JSON de cada evento por su clave
# inicial — el mismo approach, probado en producción, de `kiro-gateway/kiro/parsers.py`.

_EVENT_KEY_PATTERNS: list[bytes] = [
    b'"content":', b'"name":', b'"input":', b'"stop":',
    b'"usage":', b'"contextUsagePercentage":',
]


def _find_json_start(buf: str) -> int:
    earliest = -1
    for pat in _EVENT_KEY_PATTERNS:
        pos = buf.find("{" + pat.decode())
        if pos != -1 and (earliest == -1 or pos < earliest):
            earliest = pos
    return earliest


def _find_matching_brace(buf: str, start: int) -> int:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(buf)):
        c = buf[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


class KiroEventStreamParser:
    """Extrae eventos JSON del stream binario de Kiro sin parsear el framing formal."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[dict]:
        self._buffer += chunk.decode("utf-8", errors="ignore")
        events: list[dict] = []
        while True:
            start = _find_json_start(self._buffer)
            if start == -1:
                break
            end = _find_matching_brace(self._buffer, start)
            if end == -1:
                break
            raw = self._buffer[start:end + 1]
            self._buffer = self._buffer[end + 1:]
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return events


def parse_kiro_eventstream(raw: bytes) -> list[dict]:
    """Parsea de una sola vez un bloque completo de bytes del event stream de Kiro."""
    parser = KiroEventStreamParser()
    return parser.feed(raw)


def _sse_chunk(delta: dict, finish_reason: str | None = None) -> bytes:
    chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"


async def kiro_events_to_openai_sse(raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Traduce el stream crudo de Kiro a chunks SSE OpenAI-compatible.

    Consume bytes crudos, extrae eventos con `KiroEventStreamParser`, y emite deltas de
    contenido/tool_calls en el formato que ya esperan `routers/openai_proxy.py` y
    `services/translators.py::openai_stream_to_anthropic`.
    """
    parser = KiroEventStreamParser()
    open_tool_index: int | None = None
    tool_call_index = -1
    finish_reason = "stop"

    async for chunk in raw:
        for event in parser.feed(chunk):
            if "content" in event and "name" not in event:
                text = event.get("content", "")
                if text:
                    yield _sse_chunk({"content": text})
            elif "name" in event:
                tool_call_index += 1
                open_tool_index = tool_call_index
                yield _sse_chunk({"tool_calls": [{
                    "index": tool_call_index,
                    "id": event.get("toolUseId", ""),
                    "type": "function",
                    "function": {"name": event.get("name", ""), "arguments": ""},
                }]})
                finish_reason = "tool_calls"
            elif "input" in event and open_tool_index is not None:
                yield _sse_chunk({"tool_calls": [{
                    "index": open_tool_index,
                    "function": {"arguments": event.get("input", "")},
                }]})
            elif "stop" in event:
                open_tool_index = None

    yield _sse_chunk({}, finish_reason=finish_reason)
    yield b"data: [DONE]\n\n"
