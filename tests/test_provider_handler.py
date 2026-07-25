"""
Tests unitarios para services/provider_handler.py

Cubre: DataDrivenHandler pre_call headers, parse_error mappings, URL construction,
       set_runtime_providers, parse_model_id_prefix.
No toca la red.
"""

import pytest

from services.provider_handler import (
    DataDrivenHandler,
    build_handler,
    set_runtime_providers,
    get_handler,
    parse_model_id_prefix,
    ErrorType,
)


# ── Configs de providers (simulan DB) ────────────────────────────────────────

PROVIDER_CONFIGS = [
    {"name": "openrouter", "label": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1",
     "models_url": "https://openrouter.ai/api/v1/models",
     "auth_type": "bearer", "auth_value": "",
     "extra_headers": {"HTTP-Referer": "http://localhost:8787", "X-Title": "Infinity Provisioner"},
     "kind": "openrouter"},
    {"name": "gemini", "label": "Gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "gemini"},
    {"name": "groq", "label": "Groq",
     "base_url": "https://api.groq.com/openai/v1",
     "models_url": "https://api.groq.com/openai/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "zai", "label": "Z.AI",
     "base_url": "https://api.z.ai/api/v1",
     "models_url": "https://api.z.ai/api/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "zen", "label": "Zen",
     "base_url": "https://opencode.ai/zen/v1",
     "models_url": "https://opencode.ai/zen/v1/models",
     "auth_type": "keyless", "auth_value": "", "extra_headers": {}, "kind": "zen"},
    {"name": "kilo", "label": "Kilo",
     "base_url": "https://api.kilo.ai/api/openrouter",
     "models_url": "https://api.kilo.ai/api/openrouter/models",
     "auth_type": "static", "auth_value": "Bearer anonymous", "extra_headers": {}, "kind": "kilo"},
    {"name": "nvidia", "label": "NVIDIA",
     "base_url": "https://integrate.api.nvidia.com/v1",
     "models_url": "https://integrate.api.nvidia.com/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "nvidia"},
]


@pytest.fixture(autouse=True)
def _populate_runtime():
    """Pobla el registry runtime antes de cada test."""
    set_runtime_providers(PROVIDER_CONFIGS)


# ── Registry runtime ─────────────────────────────────────────────────────────


def test_registry_has_all_known_providers():
    from services.provider_handler import KNOWN_PROVIDERS
    assert KNOWN_PROVIDERS == {
        "openrouter", "gemini", "groq", "zai", "zen", "kilo", "nvidia"
    }


def test_get_handler_returns_data_driven():
    h = get_handler("openrouter")
    assert isinstance(h, DataDrivenHandler)
    assert h.name == "openrouter"


def test_get_handler_unknown_raises():
    with pytest.raises(KeyError):
        get_handler("nonexistent")


# ── build_handler (sin registry) ─────────────────────────────────────────────


def test_build_handler_creates_independent():
    cfg = {"name": "test", "base_url": "https://x.com/v1", "models_url": "https://x.com/v1/models"}
    h = build_handler(cfg)
    assert h.name == "test"
    assert h.base_url == "https://x.com/v1"


# ── parse_model_id_prefix ─────────────────────────────────────────────────────


def test_prefix_parses_known_provider():
    assert parse_model_id_prefix("groq/openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")


def test_prefix_returns_none_for_unknown_provider():
    assert parse_model_id_prefix("minimax/minimax-m2.5:free") == (None, "minimax/minimax-m2.5:free")


def test_prefix_no_slash_returns_none():
    assert parse_model_id_prefix("gpt-4") == (None, "gpt-4")


def test_prefix_openrouter_with_openrouter_first_segment():
    assert parse_model_id_prefix("openrouter/anthropic/claude-3.5-sonnet") == (
        "openrouter", "anthropic/claude-3.5-sonnet"
    )


# ── OpenRouterHandler (data-driven) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_openrouter_pre_call_headers():
    h = get_handler("openrouter")
    dep = {"model_id": "minimax/minimax-m2.5:free"}
    inst = {"api_key": "sk-abc"}
    pc = await h.pre_call(dep, inst, {"messages": []})
    assert pc.url == "https://openrouter.ai/api/v1/chat/completions"
    assert pc.headers["Authorization"] == "Bearer sk-abc"
    assert pc.headers["HTTP-Referer"] == "http://localhost:8787"
    assert pc.headers["X-Title"] == "Infinity Provisioner"
    assert pc.headers["Content-Type"] == "application/json"
    assert pc.body["model"] == "minimax/minimax-m2.5:free"


def test_openrouter_models_url_is_canonical():
    assert get_handler("openrouter").models_url == "https://openrouter.ai/api/v1/models"


# ── Gemini / Groq / ZAI (Bearer) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_url_and_headers():
    h = get_handler("gemini")
    pc = await h.pre_call({"model_id": "gemini-2.0-flash"}, {"api_key": "k"}, {"messages": []})
    assert pc.url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert pc.headers["Authorization"] == "Bearer k"
    assert "HTTP-Referer" not in pc.headers


@pytest.mark.asyncio
async def test_groq_url_and_headers():
    h = get_handler("groq")
    pc = await h.pre_call({"model_id": "llama-3.3-70b-versatile"}, {"api_key": "k"}, {})
    assert pc.url == "https://api.groq.com/openai/v1/chat/completions"
    assert pc.headers["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_zai_url_and_headers():
    h = get_handler("zai")
    pc = await h.pre_call({"model_id": "glm-4.7-flash"}, {"api_key": "k"}, {})
    assert pc.url == "https://api.z.ai/api/v1/chat/completions"
    assert pc.headers["Authorization"] == "Bearer k"


# ── Zen / Kilo (keyless) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zen_url_and_headers_no_auth():
    h = get_handler("zen")
    pc = await h.pre_call({"model_id": "mimo-flash-free"}, None, {})
    assert pc.url == "https://opencode.ai/zen/v1/chat/completions"
    assert "Authorization" not in pc.headers
    assert pc.headers["User-Agent"] == "Mozilla/5.0"


@pytest.mark.asyncio
async def test_kilo_url_and_headers_anonymous():
    h = get_handler("kilo")
    pc = await h.pre_call({"model_id": "openai/gpt-oss-120b"}, None, {})
    assert pc.url == "https://api.kilo.ai/api/openrouter/chat/completions"
    assert pc.headers["Authorization"] == "Bearer anonymous"
    assert pc.headers["User-Agent"] == "Mozilla/5.0"


# ── parse_error mappings ──────────────────────────────────────────────────────


def test_parse_error_429_rate_limit():
    err = get_handler("openrouter").parse_error(429, "")
    assert err.error_type == ErrorType.RATE_LIMIT
    assert err.raw_status == 429


def test_parse_error_404_model_not_found():
    err = get_handler("groq").parse_error(404, "")
    assert err.error_type == ErrorType.MODEL_NOT_FOUND


def test_parse_error_401_auth():
    err = get_handler("gemini").parse_error(401, "")
    assert err.error_type == ErrorType.AUTH_ERROR


def test_parse_error_403_auth():
    err = get_handler("zai").parse_error(403, "")
    assert err.error_type == ErrorType.AUTH_ERROR


def test_parse_error_500_server_error():
    err = get_handler("openrouter").parse_error(500, "")
    assert err.error_type == ErrorType.SERVER_ERROR


def test_parse_error_502_server_error():
    err = get_handler("kilo").parse_error(502, "")
    assert err.error_type == ErrorType.SERVER_ERROR


def test_parse_error_400_context_window():
    err = get_handler("openrouter").parse_error(400, "context length too long")
    assert err.error_type == ErrorType.CONTEXT_WINDOW_EXCEEDED


def test_parse_error_400_content_policy():
    err = get_handler("groq").parse_error(400, "content filter blocked")
    assert err.error_type == ErrorType.CONTENT_POLICY_VIOLATION


def test_parse_error_400_plain_unknown():
    err = get_handler("zai").parse_error(400, "bad request")
    assert err.error_type == ErrorType.UNKNOWN


def test_parse_error_unknown_status_unknown():
    err = get_handler("zen").parse_error(418, "I am a teapot")
    assert err.error_type == ErrorType.UNKNOWN


# ── kind=kiro ─────────────────────────────────────────────────────────────────

KIRO_CFG = {
    "name": "kiro", "label": "Kiro",
    "base_url": "https://oidc.us-east-1.amazonaws.com",
    "models_url": "", "auth_type": "oauth_device", "auth_value": "",
    "extra_headers": {}, "kind": "kiro",
}


def _kiro_instance(profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/p1"):
    import json
    return {
        "id": "kiro-1", "provider": "kiro",
        "oauth_state": json.dumps({
            "access_token": "tok-123",
            "region": "https://oidc.us-east-1.amazonaws.com",
            "profile_arn": profile_arn,
            "status": "active",
        }),
    }


@pytest.mark.asyncio
async def test_kiro_pre_call_builds_real_kiro_request():
    h = build_handler(KIRO_CFG)
    pc = await h.pre_call(
        {"model_id": "claude-sonnet-4.5"}, _kiro_instance(),
        {"messages": [{"role": "user", "content": "hola"}]},
    )
    assert pc.url == "https://runtime.us-east-1.kiro.dev/"
    assert pc.headers["Authorization"] == "Bearer tok-123"
    assert pc.headers["x-amz-target"] == "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
    assert pc.headers["Content-Type"] == "application/x-amz-json-1.0"
    assert pc.body["profileArn"] == "arn:aws:codewhisperer:us-east-1:1:profile/p1"
    current = pc.body["conversationState"]["currentMessage"]["userInputMessage"]
    assert current["content"] == "hola"
    assert current["modelId"] == "claude-sonnet-4.5"


@pytest.mark.asyncio
async def test_kiro_pre_call_rejects_oversized_payload():
    h = build_handler(KIRO_CFG)
    huge_content = "x" * 700_000
    with pytest.raises(ValueError, match="excede el límite"):
        await h.pre_call(
            {"model_id": "auto"}, _kiro_instance(),
            {"messages": [{"role": "user", "content": huge_content}]},
        )


@pytest.mark.asyncio
async def test_kiro_translate_stream_converts_event_stream_to_sse():
    h = build_handler(KIRO_CFG)

    async def raw():
        yield b'{"content":"Hola"}'

    chunks = [c async for c in h.translate_stream(raw())]
    text = b"".join(chunks).decode()
    assert '"content":"Hola"' in text
    assert "data: [DONE]" in text


@pytest.mark.asyncio
async def test_non_kiro_translate_stream_is_pass_through():
    h = get_handler("openrouter")

    async def raw():
        yield b'{"choices":[]}'

    chunks = [c async for c in h.translate_stream(raw())]
    assert chunks == [b'{"choices":[]}']


def test_kiro_response_is_stream_flag_set():
    assert build_handler(KIRO_CFG).response_is_stream is True
    assert get_handler("openrouter").response_is_stream is False
