"""
Tests para services/translators.py — dirección Anthropic → OpenAI, sin cubrir
hasta ahora (_convert_anthropic_message, anthropic_to_openai). Si esto se
rompe, Claude Code deja de poder usar tool calls vía el proxy 8788 sin que
ningún test lo detecte.
"""

import json

from services.translators import _convert_anthropic_message, anthropic_to_openai


# ── _convert_anthropic_message ───────────────────────────────────────────────


def test_convert_tool_result_with_block_list_content():
    msg = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "text", "text": "resultado de la tool"}],
            }
        ],
    }
    converted = _convert_anthropic_message(msg)
    assert converted == [{
        "role": "tool", "tool_call_id": "toolu_1", "content": "resultado de la tool",
    }]


def test_convert_tool_result_with_string_content():
    msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}],
    }
    converted = _convert_anthropic_message(msg)
    assert converted == [{"role": "tool", "tool_call_id": "toolu_2", "content": "ok"}]


def test_convert_multiple_tool_results_become_separate_messages():
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
        ],
    }
    converted = _convert_anthropic_message(msg)
    assert len(converted) == 2
    assert converted[0]["tool_call_id"] == "t1"
    assert converted[1]["tool_call_id"] == "t2"


def test_convert_assistant_tool_use_with_mixed_text():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Voy a leer el archivo"},
            {"type": "tool_use", "id": "toolu_3", "name": "read_file", "input": {"path": "a.py"}},
        ],
    }
    converted = _convert_anthropic_message(msg)
    assert len(converted) == 1
    result = converted[0]
    assert result["role"] == "assistant"
    assert result["content"] == "Voy a leer el archivo"
    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["id"] == "toolu_3"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "a.py"}


def test_convert_plain_text_blocks():
    msg = {"role": "user", "content": [{"type": "text", "text": "hola"}]}
    converted = _convert_anthropic_message(msg)
    assert converted == [{"role": "user", "content": "hola"}]


def test_convert_simple_string_content_unchanged():
    msg = {"role": "user", "content": "hola directo"}
    converted = _convert_anthropic_message(msg)
    assert converted == [{"role": "user", "content": "hola directo"}]


# ── anthropic_to_openai ──────────────────────────────────────────────────────


def test_anthropic_to_openai_system_as_block_list_flattens_to_string():
    body = {
        "system": [
            {"type": "text", "text": "Eres un asistente."},
            {"type": "text", "text": "Responde en español."},
        ],
        "messages": [{"role": "user", "content": "hola"}],
    }
    oai = anthropic_to_openai(body, target_model="test-model")
    assert oai["messages"][0] == {
        "role": "system", "content": "Eres un asistente. Responde en español.",
    }


def test_anthropic_to_openai_system_as_plain_string():
    body = {
        "system": "Eres útil",
        "messages": [{"role": "user", "content": "hola"}],
    }
    oai = anthropic_to_openai(body, target_model="test-model")
    assert oai["messages"][0] == {"role": "system", "content": "Eres útil"}


def test_anthropic_to_openai_tools_map_to_function_schema():
    body = {
        "messages": [{"role": "user", "content": "hola"}],
        "tools": [{
            "name": "read_file",
            "description": "Lee un archivo",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }],
    }
    oai = anthropic_to_openai(body, target_model="test-model")
    assert oai["tools"] == [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee un archivo",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


def test_anthropic_to_openai_sets_target_model_and_defaults():
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100, "stream": True}
    oai = anthropic_to_openai(body, target_model="minimax/minimax-m2.5:free")
    assert oai["model"] == "minimax/minimax-m2.5:free"
    assert oai["max_tokens"] == 100
    assert oai["stream"] is True
