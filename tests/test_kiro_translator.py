"""
Tests unitarios para services/kiro_translator.py

Cubre: build_kiro_payload (reglas de conversationState) y el parser heurístico de
event stream (KiroEventStreamParser / kiro_events_to_openai_sse). No toca la red.
"""

import json

import pytest

from services.kiro_translator import (
    KIRO_STATIC_MODELS,
    KiroEventStreamParser,
    build_kiro_payload,
    kiro_events_to_openai_sse,
    parse_kiro_eventstream,
)


# ── build_kiro_payload ──────────────────────────────────────────────────────


def test_build_kiro_payload_simple_conversation():
    body = {"messages": [
        {"role": "system", "content": "Eres útil."},
        {"role": "user", "content": "Hola"},
        {"role": "assistant", "content": "¿En qué te ayudo?"},
        {"role": "user", "content": "Nada, gracias"},
    ]}
    payload = build_kiro_payload(body, model_id="auto", profile_arn="arn:test")

    assert payload["profileArn"] == "arn:test"
    cs = payload["conversationState"]
    assert cs["chatTriggerType"] == "MANUAL"
    current = cs["currentMessage"]["userInputMessage"]
    assert current["content"] == "Nada, gracias"
    assert current["modelId"] == "auto"
    # El primer turno de history debe llevar el system prompt prepend
    first_history = cs["history"][0]["userInputMessage"]["content"]
    assert first_history.startswith("Eres útil.")


def test_build_kiro_payload_with_tools_and_tool_results():
    body = {
        "messages": [
            {"role": "user", "content": "¿Qué hora es en Madrid?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_time", "arguments": '{"tz": "Europe/Madrid"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "14:00"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "get_time", "description": "Da la hora", "parameters": {"type": "object"},
        }}],
    }
    payload = build_kiro_payload(body, model_id="auto", profile_arn="arn:test")
    current = payload["conversationState"]["currentMessage"]["userInputMessage"]
    ctx = current["userInputMessageContext"]
    assert ctx["tools"][0]["toolSpecification"]["name"] == "get_time"
    assert ctx["toolResults"][0]["toolUseId"] == "call_1"
    assert ctx["toolResults"][0]["content"][0]["text"] == "14:00"

    history = payload["conversationState"]["history"]
    assistant_entry = next(h for h in history if "assistantResponseMessage" in h)
    assert assistant_entry["assistantResponseMessage"]["toolUses"][0]["name"] == "get_time"


def test_build_kiro_payload_strips_tool_content_without_tools_declared():
    body = {"messages": [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "resultado"},
        {"role": "user", "content": "gracias"},
    ]}
    # Sin "tools" en el body: Kiro rechaza toolResults sin tools declarados.
    payload = build_kiro_payload(body, model_id="auto", profile_arn="arn:test")
    raw = json.dumps(payload)
    assert "toolResults" not in raw
    assert "toolUses" not in raw
    current = payload["conversationState"]["currentMessage"]["userInputMessage"]
    assert current["content"] == "gracias"


def test_build_kiro_payload_inserts_synthetic_messages_for_non_alternating_roles():
    body = {"messages": [
        {"role": "assistant", "content": "primero (no debería empezar así)"},
        {"role": "user", "content": "uno"},
        {"role": "user", "content": "dos"},
    ]}
    payload = build_kiro_payload(body, model_id="auto", profile_arn="")
    cs = payload["conversationState"]
    # El primer turno real es assistant → se antepone un user sintético.
    first = cs["history"][0]
    assert "userInputMessage" in first
    assert first["userInputMessage"]["content"] == "(empty placeholder)"
    # "uno" y "dos" son ambos user consecutivos → debe haberse insertado un
    # assistant sintético entre ellos en algún punto del history/current.
    all_entries = cs["history"] + [{"userInputMessage": cs["currentMessage"]["userInputMessage"]}]
    roles = ["userInputMessage" if "userInputMessage" in e else "assistantResponseMessage" for e in all_entries]
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1], f"roles no alternan: {roles}"


def test_build_kiro_payload_empty_content_placeholder():
    body = {"messages": [{"role": "user", "content": ""}]}
    payload = build_kiro_payload(body, model_id="auto", profile_arn="")
    current = payload["conversationState"]["currentMessage"]["userInputMessage"]
    assert current["content"] == "(empty placeholder)"


# ── Parser heurístico de event stream ───────────────────────────────────────


def test_parser_extracts_content_event():
    parser = KiroEventStreamParser()
    events = parser.feed(b'garbage-framing-bytes\xff\xfe{"content":"Hola"}more-garbage\x00')
    assert events == [{"content": "Hola"}]


def test_parser_handles_json_split_across_feeds():
    parser = KiroEventStreamParser()
    assert parser.feed(b'{"conte') == []
    events = parser.feed(b'nt":"partido"}')
    assert events == [{"content": "partido"}]


def test_parser_extracts_tool_use_sequence():
    parser = KiroEventStreamParser()
    events = parser.feed(
        b'{"name":"get_time","toolUseId":"call_1"}'
        b'{"input":"{\\"tz\\""}{"input":":\\"UTC\\"}"}'
        b'{"stop":true}'
    )
    assert events[0]["name"] == "get_time"
    assert events[1]["input"] == '{"tz"'
    assert events[-1]["stop"] is True


def test_parser_ignores_incomplete_trailing_json():
    parser = KiroEventStreamParser()
    events = parser.feed(b'{"content":"ok"}{"content":"incompl')
    assert events == [{"content": "ok"}]


def test_parse_kiro_eventstream_one_shot():
    events = parse_kiro_eventstream(b'{"content":"hola"}{"usage":42}')
    assert events == [{"content": "hola"}, {"usage": 42}]


@pytest.mark.asyncio
async def test_kiro_events_to_openai_sse_text():
    async def raw():
        yield b'{"content":"Hola"}'
        yield b'{"content":" mundo"}'

    chunks = [c async for c in kiro_events_to_openai_sse(raw())]
    text = b"".join(chunks).decode()
    assert '"content":"Hola"' in text
    assert '"content":" mundo"' in text
    assert text.strip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_kiro_events_to_openai_sse_tool_call():
    async def raw():
        yield b'{"name":"get_time","toolUseId":"call_1"}{"input":"{}"}{"stop":true}'

    chunks = [c async for c in kiro_events_to_openai_sse(raw())]
    text = b"".join(chunks).decode()
    assert '"name":"get_time"' in text
    assert '"finish_reason":"tool_calls"' in text


def test_static_models_list_not_empty():
    assert "auto" in KIRO_STATIC_MODELS
    assert len(KIRO_STATIC_MODELS) >= 5
