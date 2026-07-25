"""
Tests para services/router.py — Router.acompletion().

Cubre: acompletion con mock provider, cooldown skip, fallback chain,
       model_group_alias, fast-path single-deployment, error 503.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException

import db
from services.router import Router, RouterResponse, Deployment
from services.cooldown import CooldownMechanism
from services.provider_handler import ErrorType


def _make_mock_resp(status_code=200, body=None):
    """Crea un mock de httpx.Response que el Router espera (resp.json sync)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(body) if body else ""
    resp.json = MagicMock(return_value=body or {})
    resp.aread = AsyncMock()
    return resp


@pytest_asyncio.fixture
async def router_with_mock(tmp_path):
    """Router con DB y cooldowns en temp dir."""
    # Limpiar cooldowns globales para aïsar tests
    from pathlib import Path
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
        # Usar cooldowns temporales para aislar tests
        r._cooldown = CooldownMechanism(persist_path=test_cooldowns)
        yield r
        await r.aclose()


# ── Basic acompletion ─────────────────────────────────────────────────────────


def _ok_response(dep=None, body=None):
    """RouterResponse con body por defecto."""
    return RouterResponse(json=body or {"ok": True}, deployment_used=dep)


@pytest.mark.asyncio
async def test_acompletion_returns_json(router_with_mock):
    r = router_with_mock
    expected = {"choices": [{"message": {"content": "hello"}}]}

    async def mock_try(dep, body, stream, proxy_type="openai"):
        return _ok_response(r._deployment_to_dict(dep), expected)

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "infinity/sonnet", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )

    assert isinstance(result, RouterResponse)
    assert result.json == expected
    assert result.deployment_used is not None
    assert result.error is None


# ── Cooldown skip ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_raises_503(router_with_mock):
    r = router_with_mock
    dep_id = r._deployments_cache[0]["id"]
    await r._cooldown.mark_failure(dep_id, ErrorType.RATE_LIMIT)

    with pytest.raises(HTTPException) as exc_info:
        await r.acompletion(
            "infinity/sonnet", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )
    assert exc_info.value.status_code == 503


# ── model_group_alias ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_group_alias_resolves(router_with_mock):
    r = router_with_mock
    r._settings_cache["model_group_alias"] = {"gpt-4": "infinity/sonnet"}

    async def mock_try(dep, body, stream, proxy_type="openai"):
        return _ok_response(r._deployment_to_dict(dep))

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "gpt-4", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )

    assert result.json == {"ok": True}
    assert result.deployment_used["model_name"] == "infinity/sonnet"


# ── Fallback chain ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_tries_next_model(router_with_mock):
    r = router_with_mock

    await db.create_deployment({
        "model_name": "infinity/haiku",
        "provider": "openrouter",
        "api_instance_id": "inst1",
        "model_id": "z-ai/glm-4.7-flash",
        "weight": 1.0,
        "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 1, "enabled": 1,
    })
    # Recargar deployments desde DB
    r.invalidate_cache()
    await r._reload_cache()
    # Configurar fallback DESPUÉS de reload (para que no se sobreescriba)
    r._settings_cache["fallbacks"] = {"infinity/sonnet": ["infinity/haiku"]}

    call_count = 0

    async def mock_try(dep, body, stream, proxy_type="openai"):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # primer deployment falla
        return RouterResponse(json={"ok": True}, deployment_used=r._deployment_to_dict(dep))

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "infinity/sonnet", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )

    assert result.json == {"ok": True}
    assert call_count == 2


# ── Fast-path single deployment ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fast_path_single_deployment(router_with_mock):
    r = router_with_mock

    async def mock_try(dep, body, stream, proxy_type="openai"):
        return _ok_response(r._deployment_to_dict(dep))

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "infinity/sonnet", {"messages": []}, stream=False
        )

    assert result.json == {"ok": True}
    assert result.deployment_used["model_name"] == "infinity/sonnet"


# ── No deployments → 503 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_deployments_raises_503(router_with_mock):
    r = router_with_mock
    with pytest.raises(HTTPException) as exc_info:
        await r.acompletion(
            "infinity/nonexistent", {"messages": []}, stream=False
        )
    assert exc_info.value.status_code == 503


# ── Context window check ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_call_context_window_skip(router_with_mock):
    r = router_with_mock
    r._settings_cache["enable_pre_call_checks"] = True

    dep_id = r._deployments_cache[0]["id"]
    await db.update_deployment(dep_id, {"max_input_tokens": 10})
    r.invalidate_cache()
    await r._reload_cache()

    long_body = {"messages": [{"role": "user", "content": "x" * 100}]}

    with pytest.raises(HTTPException) as exc_info:
        await r.acompletion("infinity/sonnet", long_body, stream=False)

    assert exc_info.value.status_code == 503


# ── Token counter (optim 8) ───────────────────────────────────────────────────


def test_count_tokens_openai_shape():
    from services.router import Router
    r = Router()
    msgs = [{"role": "user", "content": "hello world"}]
    count = r._count_tokens(msgs, {})
    # hello world son ~3 tokens con cl100k_base
    assert count > 0


def test_count_tokens_anthropic_shape_has_overhead():
    from services.router import Router
    r = Router()
    msgs_openai = [{"role": "user", "content": "hello world"}]
    msgs_anth = [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]
    count_openai = r._count_tokens(msgs_openai, {})
    count_anth = r._count_tokens(msgs_anth, {})
    # Anthropic-shape con factor 1.3x debe ser mayor
    assert count_anth > count_openai


def test_count_tokens_fallback_when_no_tiktoken():
    """Si tiktoken falla, el fallback len//4 sigue funcionando."""
    from services.router import Router
    import builtins
    r = Router()
    msgs = [{"role": "user", "content": "hello"}]
    count = r._count_tokens(msgs, {})
    assert count >= 1  # nunca 0


def test_count_tokens_is_static():
    """_count_tokens es staticmethod, no necesita instancia de Router."""
    from services.router import Router
    msgs = [{"role": "user", "content": "test"}]
    count = Router._count_tokens(msgs, {})
    assert count > 0


# ── Fallbacks específicos antes que default_fallbacks ────────────────────────


@pytest.mark.asyncio
async def test_specific_fallback_tried_before_default_fallback(router_with_mock):
    """Cuando existen tanto un fallback específico para el model_name como un
    default_fallback, el específico debe intentarse primero. Sin este test una
    regresión invertiría silenciosamente la prioridad de fallback."""
    r = router_with_mock

    await db.create_deployment({
        "model_name": "infinity/haiku", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "haiku-model",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 1, "enabled": 1,
    })
    await db.create_deployment({
        "model_name": "infinity/opus", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "opus-model",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 1, "enabled": 1,
    })
    r.invalidate_cache()
    await r._reload_cache()
    r._settings_cache["fallbacks"] = {"infinity/sonnet": ["infinity/haiku"]}
    r._settings_cache["default_fallbacks"] = ["infinity/opus"]

    attempted_models = []

    async def mock_try(dep, body, stream, proxy_type="openai"):
        attempted_models.append(dep.model_name)
        # opus responde OK para cortar la cadena (default_fallbacks incluye
        # infinity/opus; si también fallara, acompletion("infinity/opus")
        # volvería a probar default_fallbacks=[opus] y recursionaría infinito).
        if dep.model_name == "infinity/opus":
            return _ok_response(r._deployment_to_dict(dep))
        return None

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "infinity/sonnet", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )

    assert result.json == {"ok": True}
    # infinity/sonnet (falla) → infinity/haiku (fallback específico, falla) →
    # infinity/opus (default_fallback, responde) — en ese orden.
    assert attempted_models.index("infinity/haiku") < attempted_models.index("infinity/opus")


# ── model_group_alias apuntando a un tier sin deployments ────────────────────


@pytest.mark.asyncio
async def test_alias_to_empty_tier_falls_back_using_resolved_name(router_with_mock):
    """Si el alias resuelve a un model_name sin ningún deployment, el fallback
    debe intentarse con el NOMBRE RESUELTO, no con el alias original — de lo
    contrario los fallbacks configurados para el tier resuelto nunca se
    encontrarían."""
    r = router_with_mock
    r._settings_cache["model_group_alias"] = {"gpt-4": "infinity/nonexistent-tier"}
    r._settings_cache["fallbacks"] = {"infinity/nonexistent-tier": ["infinity/sonnet"]}

    async def mock_try(dep, body, stream, proxy_type="openai"):
        return _ok_response(r._deployment_to_dict(dep))

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        result = await r.acompletion(
            "gpt-4", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
        )

    assert result.json == {"ok": True}
    assert result.deployment_used["model_name"] == "infinity/sonnet"


# ── Ciclo en default_fallbacks no debe recursionar infinitamente ────────────


@pytest.mark.asyncio
async def test_default_fallback_pointing_to_itself_raises_503_not_recursion(router_with_mock):
    """default_fallbacks que apunta al propio model_name (o a un ciclo) causaba
    RecursionError: acompletion(A) falla → _try_fallbacks prueba default_fallbacks=[A]
    → acompletion(A) de nuevo → ... sin límite. Debe terminar en 503, no reventar."""
    r = router_with_mock
    r._settings_cache["default_fallbacks"] = ["infinity/sonnet"]

    call_count = 0

    async def mock_try(dep, body, stream, proxy_type="openai"):
        nonlocal call_count
        call_count += 1
        return None  # siempre falla

    with patch.object(r, "_try_deployment", side_effect=mock_try):
        with pytest.raises(HTTPException) as exc_info:
            await r.acompletion(
                "infinity/sonnet", {"messages": [{"role": "user", "content": "hi"}]}, stream=False
            )

    assert exc_info.value.status_code == 503
    # El único deployment de infinity/sonnet se intenta una sola vez — el
    # ciclo del default_fallback se corta antes de reintentarlo.
    assert call_count == 1


# ── Parity hardening: pool HTTP/2 ───────────────────────────────────────────────


# ── Reintentos en-llamada ante error transitorio (concurrencia/5xx) ────────────


def _err_resp(status_code, text="boom", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    resp.aread = AsyncMock()
    return resp


@pytest.mark.asyncio
async def test_incall_retry_recovers_on_transient_then_succeeds(router_with_mock):
    """Un 500 transitorio se reintenta en-llamada; el 2º intento (200) tiene éxito."""
    r = router_with_mock
    r._num_incall_retries = 2
    r._incall_retry_base = 0  # sin sleep real en test
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    r._client.send = AsyncMock(side_effect=[_err_resp(500), _make_mock_resp(200, {"ok": True})])

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    assert result is not None
    assert result.error is None
    assert result.json == {"ok": True}
    assert r._client.send.call_count == 2
    assert r._cooldown.is_cooling_down(dep_id) is False


@pytest.mark.asyncio
async def test_incall_retry_exhausts_then_cooldowns_once(router_with_mock):
    """Si el error transitorio persiste, agota reintentos y marca cooldown una sola vez."""
    r = router_with_mock
    r._num_incall_retries = 2
    r._incall_retry_base = 0
    dep = r._deployments_cache[0]

    r._client.send = AsyncMock(return_value=_err_resp(500))

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        result = await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )

    assert result is not None
    assert result.error is not None
    assert r._client.send.call_count == 3  # 1 intento + 2 reintentos
    spy.assert_called_once()  # cooldown solo al agotar los reintentos


@pytest.mark.asyncio
async def test_unknown_400_is_not_retried(router_with_mock):
    """Un 400 UNKNOWN (payload malo) NO se reintenta: mismo body fallará igual."""
    r = router_with_mock
    r._num_incall_retries = 2
    r._incall_retry_base = 0
    dep = r._deployments_cache[0]

    r._client.send = AsyncMock(return_value=_err_resp(400, "malformed request body"))

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    assert result is not None
    assert result.error is not None
    assert r._client.send.call_count == 1  # sin reintentos


def test_concurrency_error_body_classified_as_rate_limit():
    """'ResourceExhausted / request limit reached' llega como 400 pero es transitorio
    → debe clasificarse RATE_LIMIT (retryable), no UNKNOWN."""
    from services.provider_handler import _parse_generic_error
    body = "ResourceExhausted: Worker local total request limit reached (33/32)"
    assert _parse_generic_error(400, body).error_type == ErrorType.RATE_LIMIT
    assert _parse_generic_error(500, "model overloaded").error_type == ErrorType.RATE_LIMIT


# ── Limitador de concurrencia (max_parallel_requests) ─────────────────────────


def test_concurrency_slot_primitives():
    """try_acquire respeta el tope y release devuelve el hueco."""
    r = Router()
    assert r._try_acquire_slot(1, 2) is True
    assert r._try_acquire_slot(1, 2) is True
    assert r._try_acquire_slot(1, 2) is False  # lleno (2/2)
    r._release_slot(1)
    assert r._try_acquire_slot(1, 2) is True    # hueco liberado


@pytest.mark.asyncio
async def test_max_parallel_requests_serializes_concurrent_calls(router_with_mock):
    """Con max_parallel_requests=1, tres llamadas concurrentes nunca solapan en el
    upstream: la #2 y #3 hacen cola hasta que se libera el hueco."""
    r = router_with_mock
    r._num_incall_retries = 0
    dep = Deployment.from_row({**r._deployments_cache[0], "max_parallel_requests": 1})

    concurrent = 0
    max_seen = 0

    async def slow_send(*args, **kwargs):
        nonlocal concurrent, max_seen
        concurrent += 1
        max_seen = max(max_seen, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return _make_mock_resp(200, {"ok": True})

    r._client.send = slow_send

    results = await asyncio.gather(*[
        r._try_deployment(dep, {"messages": [{"role": "user", "content": "hi"}]}, stream=False)
        for _ in range(3)
    ])

    assert all(res is not None and res.json == {"ok": True} for res in results)
    assert max_seen == 1  # nunca 2 peticiones a la vez en el upstream


@pytest.mark.asyncio
async def test_max_parallel_requests_zero_is_unlimited(router_with_mock):
    """max_parallel_requests=0 (default) no limita: no ocupa huecos."""
    r = router_with_mock
    r._num_incall_retries = 0
    dep = Deployment.from_row({**r._deployments_cache[0], "max_parallel_requests": 0})
    r._client.send = AsyncMock(return_value=_make_mock_resp(200, {"ok": True}))

    result = await r._try_deployment(
        dep, {"messages": [{"role": "user", "content": "hi"}]}, stream=False,
    )
    assert result is not None and result.json == {"ok": True}
    assert r._inflight == {}  # no se registró ningún hueco


@pytest.mark.asyncio
async def test_http_pool_keepalive_matches_max_connections(router_with_mock):
    r = router_with_mock
    limits = r._client._transport._pool._max_keepalive_connections
    max_conn = r._client._transport._pool._max_connections
    assert limits == max_conn


# ── Parity hardening: is_single_deployment propagado desde el Router ───────────


@pytest.mark.asyncio
async def test_try_deployment_single_dep_rate_limit_does_not_cooldown(router_with_mock):
    """Único deployment habilitado para infinity/sonnet + 429 → no cooldownea."""
    r = router_with_mock
    r._num_incall_retries = 0  # aísla la semántica de cooldown de un solo intento
    r._cooldown.update_config(allowed_fails={ErrorType.RATE_LIMIT: 1})
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = MagicMock()
    resp.status_code = 429
    resp.text = ""
    resp.headers = {}
    resp.aread = AsyncMock()
    r._client.send = AsyncMock(return_value=resp)

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    assert result is not None
    assert result.error is not None
    assert r._cooldown.is_cooling_down(dep_id) is False


@pytest.mark.asyncio
async def test_try_deployment_multi_dep_rate_limit_cooldowns(router_with_mock):
    """Con ≥2 deployments habilitados para el mismo model_name, 429 sí cooldownea."""
    r = router_with_mock
    r._num_incall_retries = 0  # aísla la semántica de cooldown de un solo intento
    r._cooldown.update_config(allowed_fails={ErrorType.RATE_LIMIT: 1})
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    await db.create_deployment({
        "model_name": "infinity/sonnet", "provider": "openrouter",
        "api_instance_id": "inst1", "model_id": "some/other-model",
        "weight": 1.0, "rpm": 0, "tpm": 0, "max_input_tokens": 0,
        "order": 2, "enabled": 1,
    })
    r.invalidate_cache()
    await r._reload_cache()

    resp = MagicMock()
    resp.status_code = 429
    resp.text = ""
    resp.headers = {}
    resp.aread = AsyncMock()
    r._client.send = AsyncMock(return_value=resp)

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    assert result is not None
    assert result.error is not None
    assert r._cooldown.is_cooling_down(dep_id) is True


# ── Parity hardening: UNKNOWN no cuenta para cooldown ───────────────────────────


@pytest.mark.asyncio
async def test_try_deployment_unknown_400_does_not_cooldown(router_with_mock):
    r = router_with_mock
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = MagicMock()
    resp.status_code = 400
    resp.text = "malformed request body"
    resp.headers = {}
    resp.aread = AsyncMock()
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        result = await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )

        assert result is not None
        assert result.error is not None
        spy.assert_not_called()
        assert r._cooldown.is_cooling_down(dep_id) is False


# ── Parity hardening: Retry-After propagado a mark_failure ─────────────────────


@pytest.mark.asyncio
async def test_try_deployment_forwards_retry_after_header(router_with_mock):
    r = router_with_mock
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = MagicMock()
    resp.status_code = 429
    resp.text = ""
    resp.headers = {"retry-after": "7"}
    resp.aread = AsyncMock()
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )
        spy.assert_called_once_with(
            dep_id, ErrorType.RATE_LIMIT, is_single_deployment=True, retry_after=7.0,
        )


@pytest.mark.asyncio
async def test_try_deployment_ignores_out_of_range_retry_after(router_with_mock):
    r = router_with_mock
    r._num_incall_retries = 0  # aísla el parseo de retry-after de un solo intento
    dep = r._deployments_cache[0]
    dep_id = dep["id"]

    resp = MagicMock()
    resp.status_code = 429
    resp.text = ""
    resp.headers = {"retry-after": "3600"}
    resp.aread = AsyncMock()
    r._client.send = AsyncMock(return_value=resp)

    with patch.object(r._cooldown, "mark_failure", wraps=r._cooldown.mark_failure) as spy:
        await r._try_deployment(
            Deployment.from_row(dep),
            {"messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )
        spy.assert_called_once_with(
            dep_id, ErrorType.RATE_LIMIT, is_single_deployment=True, retry_after=None,
        )


# ── Errores en banda dentro del stream (200 OK + {"error":…} a mitad) ──────────


def test_inband_stream_error_detects_error_payload():
    """Un chunk SSE cuyo JSON trae `error` de primer nivel se detecta; el texto
    generado por el modelo que *habla* de errores, no (sin falsos positivos)."""
    from services.router import _inband_stream_error

    err = (b'data: {"error": {"message": "ResourceExhausted: Worker local total '
           b'request limit reached (33/32)"}}\n\n')
    assert "ResourceExhausted" in _inband_stream_error(err)

    ok = b'data: {"choices":[{"delta":{"content":"hubo un error en tu codigo"}}]}\n\n'
    assert _inband_stream_error(ok) is None
    assert _inband_stream_error(b"data: [DONE]\n\n") is None
    assert _inband_stream_error(b"data: no-json error\n\n") is None


def _stream_resp(chunks):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.aclose = AsyncMock()

    async def _iter():
        for c in chunks:
            yield c

    resp.aiter_bytes = MagicMock(side_effect=lambda: _iter())
    return resp


@pytest.mark.asyncio
async def test_inband_error_in_first_chunk_falls_back_without_reaching_client(router_with_mock):
    """Si el error en banda llega en el primer chunk, no se ha emitido nada al
    cliente: se trata como fallo pre-stream (cooldown + None) para que el caller
    pruebe otro deployment, en vez de reenviar el mensaje del upstream."""
    r = router_with_mock
    r._num_incall_retries = 0
    dep = r._deployments_cache[0]
    err = b'data: {"error": {"message": "ResourceExhausted: request limit reached (33/32)"}}\n\n'
    r._client.send = AsyncMock(return_value=_stream_resp([err]))

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=True,
    )

    assert result is None


@pytest.mark.asyncio
async def test_inband_error_mid_stream_interrupts_instead_of_forwarding(router_with_mock):
    """A mitad de respuesta el chunk de error NO se reenvía: se corta con
    UpstreamStreamInterrupted para que _resilient_stream retome con otro deployment."""
    from services.router import UpstreamStreamInterrupted

    r = router_with_mock
    r._num_incall_retries = 0
    dep = r._deployments_cache[0]
    good = b'data: {"choices":[{"delta":{"content":"hola"}}]}\n\n'
    err = b'data: {"error": {"message": "ResourceExhausted: request limit reached (33/32)"}}\n\n'
    r._client.send = AsyncMock(return_value=_stream_resp([good, err]))

    result = await r._try_deployment(
        Deployment.from_row(dep),
        {"messages": [{"role": "user", "content": "hi"}]},
        stream=True,
    )

    assert result is not None and result.stream is not None
    seen = []
    with pytest.raises(UpstreamStreamInterrupted):
        async for c in result.stream:
            seen.append(c)

    assert seen == [good]  # el chunk de error nunca sale hacia el cliente
