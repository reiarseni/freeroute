"""
Tests para el cambio fix-streaming-midstream-failures.

Cubre: excepción mid-stream en _try_deployment (router), fallo de lectura de body
       no-streaming, regresión de reintento pre-primer-byte, mapeo finish_reason →
       stop_reason, cierre limpio del evento error en ambos traductores/proxies.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
import pytest
import pytest_asyncio

import db
from services.cooldown import CooldownMechanism
from services.provider_handler import ErrorType
from services.router import (
    Router,
    Deployment,
    RouterResponse,
    UpstreamStreamInterrupted,
    MAX_STREAM_RECOVERIES,
)
from services.translators import (
    _map_stop_reason,
    openai_stream_to_anthropic,
    openai_to_anthropic,
)


def _make_streaming_resp(chunks: list[bytes], fail_after: int | None = None):
    """Mock de httpx.Response cuyo aiter_bytes() corta con una excepción tras N chunks."""
    resp = MagicMock()
    resp.status_code = 200

    async def _aiter():
        for i, c in enumerate(chunks):
            if fail_after is not None and i == fail_after:
                raise httpx.ReadError("connection reset")
            yield c

    resp.aiter_bytes = _aiter
    return resp


@pytest_asyncio.fixture
async def router_with_mock(tmp_path):
    """Router con DB y cooldowns en temp dir (mismo patrón que test_router_core.py)."""
    global_cd = Path.home() / ".infinity-provisioner-cooldowns.json"
    if global_cd.exists():
        global_cd.unlink()
    test_db = tmp_path / "test.db"
    test_cooldowns = tmp_path / "cooldowns.json"
    with patch("db.DB_PATH", test_db):
        await db.init_db()
        await db.upsert_instance({
            "id": "inst1", "name": "Test", "provider": "openrouter",
            "api_key": "sk-test", "is_free": 1,
        })
        await db.create_deployment({
            "model_name": "infinity/sonnet", "provider": "openrouter",
            "api_instance_id": "inst1", "model_id": "minimax/minimax-m2.5:free",
            "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
            "order": 1, "enabled": 1,
        })
        r = Router()
        await r._ensure_init()
        r._cooldown = CooldownMechanism(persist_path=test_cooldowns)
        yield r
        await r.aclose()


# ── 1.4 — corte mid-stream levanta UpstreamStreamInterrupted ────────────────


@pytest.mark.asyncio
async def test_midstream_cut_raises_and_marks_cooldown(router_with_mock):
    r = router_with_mock
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = _make_streaming_resp([b"chunk1", b"chunk2", b"chunk3"], fail_after=2)
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        result = await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=True,
        )

        assert result is not None
        assert result.error is None
        assert result.stream is not None

        collected = []
        with pytest.raises(UpstreamStreamInterrupted):
            async for chunk in result.stream:
                collected.append(chunk)

        assert collected == [b"chunk1", b"chunk2"]
        spy.assert_called_once_with(dep_id, ErrorType.TIMEOUT, is_single_deployment=True)

    logs = await db.get_logs(limit=10)
    assert any(log["status_code"] == 0 for log in logs)


# ── 1.5 — fallo de lectura del body no-streaming reintenta sin propagar ─────


@pytest.mark.asyncio
async def test_nonstream_body_read_failure_returns_none(router_with_mock):
    r = router_with_mock
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = MagicMock()
    resp.status_code = 200
    resp.aread = AsyncMock(side_effect=httpx.ReadError("connection reset"))
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        result = await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )

        assert result is None
        spy.assert_called_once_with(dep_id, ErrorType.TIMEOUT, is_single_deployment=True)


# ── 1.6 — regresión: fallo antes del threshold sigue reintentando igual ─────


@pytest.mark.asyncio
async def test_pre_threshold_failure_still_retries(router_with_mock):
    """El primer chunk nunca llega (hang) — el hanging_threshold sigue disparando
    el reintento transparente exactamente igual que antes de este cambio."""
    r = router_with_mock
    dep = r._deployments_cache[0]
    dep_id = dep["id"]
    r._hanging_threshold = 0.05

    async def _aiter_hangs():
        await asyncio.sleep(10)
        yield b"never"  # pragma: no cover — nunca se alcanza

    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_bytes = _aiter_hangs
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        result = await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=True,
        )

        assert result is None
        spy.assert_called_once_with(dep_id, ErrorType.TIMEOUT, is_single_deployment=True)


# ── 2.6 / 2.7 — mapeo finish_reason → stop_reason ────────────────────────────


def test_map_stop_reason_length_with_tool_calls_wins_over_tool_use():
    assert _map_stop_reason("length", has_tool_calls=True) == "max_tokens"


def test_map_stop_reason_length_without_tool_calls():
    assert _map_stop_reason("length", has_tool_calls=False) == "max_tokens"


def test_map_stop_reason_tool_use():
    assert _map_stop_reason("tool_calls", has_tool_calls=True) == "tool_use"


def test_map_stop_reason_content_filter():
    assert _map_stop_reason("content_filter", has_tool_calls=False) == "refusal"


def test_map_stop_reason_default_end_turn():
    assert _map_stop_reason("stop", has_tool_calls=False) == "end_turn"
    assert _map_stop_reason(None, has_tool_calls=False) == "end_turn"


def test_openai_to_anthropic_max_tokens_with_truncated_tool_call():
    data = {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{
            "finish_reason": "length",
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path": "foo.'},
                }],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = openai_to_anthropic(data)
    assert result["stop_reason"] == "max_tokens"
    # El input queda vacío porque el JSON incompleto no parsea — se entrega tal cual (dict vacío
    # de fallback), sin que el proxy intente reparar el JSON truncado.
    assert result["content"][0]["type"] == "tool_use"


def test_openai_to_anthropic_max_tokens_without_tool_calls():
    data = {
        "id": "chatcmpl-2",
        "model": "test-model",
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "texto truncado"},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = openai_to_anthropic(data)
    assert result["stop_reason"] == "max_tokens"


# ── 2.8 — UpstreamStreamInterrupted a mitad de streaming Anthropic ──────────


async def _bytes_gen(lines: list[bytes], fail_with: Exception | None = None):
    for line in lines:
        yield line
    if fail_with is not None:
        raise fail_with


@pytest.mark.asyncio
async def test_openai_stream_to_anthropic_emits_error_on_interruption():
    dep = Deployment(
        id=1, model_name="infinity/sonnet", provider="openrouter",
        api_instance_id="inst1", model_id="m", weight=1.0, rpm=0, tpm=0,
        max_input_tokens=0, max_parallel_requests=0, order=1, enabled=True,
    )

    chunk1 = json.dumps({
        "choices": [{"delta": {"content": "hola"}, "finish_reason": None}]
    }).encode()

    async def _byte_stream():
        yield b"data: " + chunk1 + b"\n\n"
        raise UpstreamStreamInterrupted(deployment=dep)

    events = []
    async for event in openai_stream_to_anthropic(_byte_stream()):
        events.append(event)

    joined = "".join(events)
    assert "content_block_stop" in joined
    assert "event: error" in joined
    assert '"type":"error"' in joined or '"type": "error"' in joined
    assert "message_stop" not in joined
    assert "message_delta" not in joined


@pytest.mark.asyncio
async def test_openai_stream_to_anthropic_normal_completion_has_no_error():
    chunk1 = json.dumps({
        "choices": [{"delta": {"content": "hola"}, "finish_reason": "stop"}]
    }).encode()

    async def _byte_stream():
        yield b"data: " + chunk1 + b"\n\n"
        yield b"data: [DONE]\n\n"

    events = []
    async for event in openai_stream_to_anthropic(_byte_stream()):
        events.append(event)

    joined = "".join(events)
    assert "event: error" not in joined
    assert "message_stop" in joined


# ── 3.3 / 3.4 — end-to-end proxies con upstream interrumpido ────────────────


@pytest.mark.asyncio
async def test_anthropic_proxy_stream_closes_clean_on_interruption():
    import httpx as httpx_module
    from fastapi import FastAPI
    from routers import anthropic_proxy
    from services.router import RouterResponse, Deployment

    dep = Deployment(
        id=1, model_name="infinity/sonnet", provider="openrouter",
        api_instance_id="inst1", model_id="m", weight=1.0, rpm=0, tpm=0,
        max_input_tokens=0, max_parallel_requests=0, order=1, enabled=True,
    )

    chunk1 = json.dumps({
        "choices": [{"delta": {"content": "hola"}, "finish_reason": None}]
    }).encode()

    async def _stream():
        yield b"data: " + chunk1 + b"\n\n"
        raise UpstreamStreamInterrupted(deployment=dep)

    app = FastAPI()
    app.include_router(anthropic_proxy.router)

    async def _mock_acompletion(model_name, body, stream=False, **kwargs):
        return RouterResponse(stream=_stream(), deployment_used={"provider": "openrouter", "model_id": "m"})

    with patch.object(anthropic_proxy.app_router, "acompletion", side_effect=_mock_acompletion):
        transport = httpx_module.ASGITransport(app=app)
        async with httpx_module.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST", "/v1/messages",
                json={"model": "claude-sonnet-4-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            ) as resp:
                body = b""
                async for b_chunk in resp.aiter_bytes():
                    body += b_chunk

    text = body.decode()
    assert "event: error" in text
    assert "message_stop" not in text


@pytest.mark.asyncio
async def test_openai_proxy_stream_closes_clean_on_interruption():
    import httpx as httpx_module
    from fastapi import FastAPI
    from routers import openai_proxy
    from services.router import RouterResponse, Deployment

    dep = Deployment(
        id=1, model_name="infinity/sonnet", provider="openrouter",
        api_instance_id="inst1", model_id="m", weight=1.0, rpm=0, tpm=0,
        max_input_tokens=0, max_parallel_requests=0, order=1, enabled=True,
    )

    chunk1 = json.dumps({
        "choices": [{"delta": {"content": "hola"}, "finish_reason": None}]
    }).encode()

    async def _stream():
        yield b"data: " + chunk1 + b"\n\n"
        raise UpstreamStreamInterrupted(deployment=dep)

    app = FastAPI()
    app.include_router(openai_proxy.router)

    async def _mock_acompletion(model_name, body, stream=False, **kwargs):
        return RouterResponse(stream=_stream(), deployment_used={"provider": "openrouter", "model_id": "m"})

    with patch.object(openai_proxy.app_router, "acompletion", side_effect=_mock_acompletion):
        transport = httpx_module.ASGITransport(app=app)
        async with httpx_module.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST", "/v1/chat/completions",
                json={"model": "infinity/sonnet", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            ) as resp:
                body = b""
                async for b_chunk in resp.aiter_bytes():
                    body += b_chunk

    text = body.decode()
    assert '"error"' in text
    assert "[DONE]" not in text


# ── 3.x — recuperación de stream cortado (_resilient_stream) ─────────────────


def _dummy_deployment() -> Deployment:
    return Deployment.from_row({
        "id": 1, "model_name": "infinity/sonnet", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "m",
    })


@pytest.mark.asyncio
async def test_resilient_stream_recovers_after_interruption():
    """Al cortarse el stream, emite la marca y empalma el stream del reintento."""
    r = Router()
    dep = _dummy_deployment()

    async def first():
        yield b"chunk1"
        yield b"chunk2"
        raise UpstreamStreamInterrupted(deployment=dep)

    async def second():
        yield b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def fake_acompletion(model_name, body, stream=True, _visited=None, proxy_type="openai"):
        return RouterResponse(stream=second())

    r._acompletion = fake_acompletion

    out = [c async for c in r._resilient_stream(first(), "infinity/sonnet", {}, "openai")]

    assert out[:2] == [b"chunk1", b"chunk2"]
    assert any(b"reintentando" in c for c in out)          # marca visible
    assert out[-1] == b"data: [DONE]\n\n"                   # segundo stream empalmado


@pytest.mark.asyncio
async def test_resilient_stream_reraises_after_exhausting_recoveries():
    """Si todos los reintentos también se cortan, relanza para que el proxy muestre error."""
    r = Router()
    dep = _dummy_deployment()

    async def always_break():
        yield b"x"
        raise UpstreamStreamInterrupted(deployment=dep)

    calls = 0

    async def fake_acompletion(model_name, body, stream=True, _visited=None, proxy_type="openai"):
        nonlocal calls
        calls += 1
        return RouterResponse(stream=always_break())

    r._acompletion = fake_acompletion

    markers = 0
    with pytest.raises(UpstreamStreamInterrupted):
        async for c in r._resilient_stream(always_break(), "infinity/sonnet", {}, "openai"):
            if b"reintentando" in c:
                markers += 1

    assert calls == MAX_STREAM_RECOVERIES
    assert markers == MAX_STREAM_RECOVERIES


@pytest.mark.asyncio
async def test_resilient_stream_reraises_when_no_provider_available():
    """Si el reintento no encuentra proveedor (HTTPException 503), relanza el corte original."""
    r = Router()
    dep = _dummy_deployment()

    async def first():
        yield b"a"
        raise UpstreamStreamInterrupted(deployment=dep)

    async def fake_acompletion(model_name, body, stream=True, _visited=None, proxy_type="openai"):
        raise HTTPException(503, detail="todos fallaron")

    r._acompletion = fake_acompletion

    out = []
    with pytest.raises(UpstreamStreamInterrupted):
        async for c in r._resilient_stream(first(), "infinity/sonnet", {}, "openai"):
            out.append(c)

    assert out[0] == b"a"


@pytest.mark.asyncio
async def test_resilient_stream_passthrough_when_no_interruption():
    """Sin corte, reenvía los chunks intactos y no llama al reintento."""
    r = Router()

    async def clean():
        yield b"a"
        yield b"b"

    async def fail_acompletion(*args, **kwargs):  # pragma: no cover — no debe llamarse
        raise AssertionError("no debería reintentar sin corte")

    r._acompletion = fail_acompletion

    out = [c async for c in r._resilient_stream(clean(), "infinity/sonnet", {}, "openai")]
    assert out == [b"a", b"b"]


# ── Terminación del stream en el proxy OpenAI (8787) ──────────────────────────
#
# Sin `data: [DONE]` el cliente ve cerrarse la conexión HTTP a mitad y lo reporta
# como "Streaming response failed", aunque la respuesta esté entera.


async def _collect_stream_gen(chunks, exc=None):
    """Ejecuta el stream_gen del proxy OpenAI sobre `chunks` y devuelve el SSE."""
    from routers import openai_proxy

    async def _upstream():
        for c in chunks:
            yield c
        if exc is not None:
            raise exc

    result = MagicMock()
    result.error = None
    result.stream = _upstream()

    with patch.object(openai_proxy.app_router, "acompletion", AsyncMock(return_value=result)):
        request = MagicMock()
        request.json = AsyncMock(return_value={"model": "infinity/sonnet", "stream": True})
        response = await openai_proxy.chat_completions(request)
        return "".join([part async for part in response.body_iterator])


@pytest.mark.asyncio
async def test_stream_always_terminated_with_done():
    """El upstream cierra sin [DONE]: el proxy lo añade igualmente."""
    out = await _collect_stream_gen([
        b'data: {"choices":[{"delta":{"content":"hola"}}]}\n\n',
    ])
    assert "hola" in out
    assert out.endswith("data: [DONE]\n\n")
    assert out.count("[DONE]") == 1


@pytest.mark.asyncio
async def test_done_from_upstream_not_duplicated():
    out = await _collect_stream_gen([
        b'data: {"choices":[{"delta":{"content":"hola"}}]}\n\ndata: [DONE]\n\n',
    ])
    assert out.count("[DONE]") == 1


@pytest.mark.asyncio
async def test_trailing_line_without_newline_is_flushed():
    """Último chunk sin salto final: antes se quedaba en el buffer y se perdía."""
    out = await _collect_stream_gen([
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"final"}},{"finish_reason":"stop"}]}',
    ])
    assert "final" in out
    assert out.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_interrupted_stream_is_not_terminated():
    """Tras agotar las recuperaciones se emite el error pero NO [DONE]: la falta de
    terminador es la señal de truncamiento que el cliente debe reportar."""
    from services.router import UpstreamStreamInterrupted

    dep = MagicMock()
    dep.id = 1
    out = await _collect_stream_gen(
        [b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'],
        exc=UpstreamStreamInterrupted(deployment=dep),
    )
    assert "upstream stream interrupted" in out
    assert "[DONE]" not in out


@pytest.mark.asyncio
async def test_unexpected_error_reported_in_band():
    out = await _collect_stream_gen(
        [b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'],
        exc=RuntimeError("boom"),
    )
    assert "proxy stream error: boom" in out
    assert "[DONE]" not in out
