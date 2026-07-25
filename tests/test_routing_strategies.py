"""
Tests para services/routing_strategies.py — RoutingStrategy.select().

Cubre: positional ordering, weighted distribution, latency preference,
       in-flight tracking, <1ms por strategy con 10 deployments.
"""

import time

import pytest
import pytest_asyncio
from unittest.mock import patch

from services.routing_strategies import (
    PositionalStrategy,
    SimpleShuffleStrategy,
    LatencyBasedStrategy,
    LeastBusyStrategy,
    RoutingContext,
)
from services.router import Router, RouterResponse, Deployment
from services.cooldown import CooldownMechanism, _bucket
from services.provider_handler import ErrorType


def _make_deps(count=3, base_order=1):
    """Crea lista de deployments mock para tests."""
    return [
        {"id": i + 1, "order": base_order + i, "weight": 1.0, "model_id": f"m{i}"}
        for i in range(count)
    ]


def _ctx():
    return RoutingContext(model_name="test", original_model="test", body={})


# ── PositionalStrategy ────────────────────────────────────────────────────────


def test_positional_selects_lowest_order():
    deps = [{"id": 3, "order": 3}, {"id": 1, "order": 1}, {"id": 2, "order": 2}]
    s = PositionalStrategy()
    result = s.select(deps, _ctx())
    assert result["id"] == 1


def test_positional_tie_broken_by_stable_sort():
    # PositionalStrategy ordena solo por `order`. Con mismo order,
    # el sort estable preserva el orden original (deps ya vienen con id 5 primero).
    deps = [{"id": 5, "order": 1}, {"id": 3, "order": 1}]
    s = PositionalStrategy()
    result = s.select(deps, _ctx())
    assert result["id"] == 5  # sort estable, primero en la lista


def test_positional_with_10_deps_under_1ms():
    deps = _make_deps(10, base_order=10)
    s = PositionalStrategy()
    start = time.perf_counter_ns()
    for _ in range(1000):
        s.select(deps, _ctx())
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    assert elapsed_ms < 10  # 1000 calls < 10ms → 1 call < 1ms


# ── SimpleShuffleStrategy ─────────────────────────────────────────────────────


def test_simple_shuffle_uniform_distribution():
    s = SimpleShuffleStrategy()
    deps = [{"id": 1, "weight": 1.0}, {"id": 2, "weight": 1.0}, {"id": 3, "weight": 1.0}]
    counts = {1: 0, 2: 0, 3: 0}
    N = 3000
    for _ in range(N):
        result = s.select(deps, _ctx())
        counts[result["id"]] += 1
    # Uniform: cada uno ~33% ±10%
    for dep_id, count in counts.items():
        ratio = count / N
        assert 0.20 < ratio < 0.45, f"dep {dep_id}: {ratio:.2%}"


def test_simple_shuffle_heavy_weight_favored():
    s = SimpleShuffleStrategy()
    deps = [{"id": 1, "weight": 0.1}, {"id": 2, "weight": 0.9}]
    counts = {1: 0, 2: 0}
    N = 1000
    for _ in range(N):
        result = s.select(deps, _ctx())
        counts[result["id"]] += 1
    # Dep 2 should be ~90%
    assert counts[2] > counts[1] * 3


def test_simple_shuffle_under_1ms():
    deps = _make_deps(10, base_order=1)
    s = SimpleShuffleStrategy()
    start = time.perf_counter_ns()
    for _ in range(1000):
        s.select(deps, _ctx())
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    assert elapsed_ms < 10


# ── LatencyBasedStrategy ──────────────────────────────────────────────────────


def test_latency_prefers_lowest_avg():
    s = LatencyBasedStrategy(maxlen=100, ttl_seconds=300)
    s.record(1, 500.0)  # 500ms avg
    s.record(2, 100.0)  # 100ms avg
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}]
    result = s.select(deps, _ctx())
    assert result["id"] == 2


def test_latency_no_data_fallback_to_median():
    s = LatencyBasedStrategy(maxlen=100, ttl_seconds=300)
    # Sin datos para ninguno → mediana por order
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}, {"id": 3, "order": 3}]
    result = s.select(deps, _ctx())
    assert result["id"] == 2  # mediana de [1,2,3]


def test_latency_expired_data_ignored():
    s = LatencyBasedStrategy(maxlen=100, ttl_seconds=0.01)
    s.record(1, 100.0)
    import time
    time.sleep(0.02)  # TTL expirado
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}]
    result = s.select(deps, _ctx())
    assert result["id"] == 2  # fallback a mediana


def test_latency_under_1ms():
    s = LatencyBasedStrategy()
    for i in range(1, 11):
        s.record(i, float(i * 100))
    deps = [{"id": i, "order": i} for i in range(1, 11)]
    start = time.perf_counter_ns()
    for _ in range(1000):
        s.select(deps, _ctx())
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    assert elapsed_ms < 100  # 1000 calls < 100ms → 1 call < 1ms


# ── LeastBusyStrategy ─────────────────────────────────────────────────────────


def test_least_busy_selects_lowest_inflight():
    s = LeastBusyStrategy()
    s._in_flight = {1: 5, 2: 1, 3: 3}
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}, {"id": 3, "order": 3}]
    result = s.select(deps, _ctx())
    assert result["id"] == 2


def test_least_busy_tie_broken_by_order():
    s = LeastBusyStrategy()
    # Todos con 0 in-flight → tie → order
    deps = [{"id": 3, "order": 3}, {"id": 1, "order": 1}, {"id": 2, "order": 2}]
    result = s.select(deps, _ctx())
    assert result["id"] == 1


def test_least_busy_increment_decrement():
    s = LeastBusyStrategy()
    s.increment(1)
    s.increment(1)
    s.increment(2)
    assert s._in_flight[1] == 2
    assert s._in_flight[2] == 1
    s.decrement(1)
    assert s._in_flight[1] == 1
    s.decrement(1)
    assert s._in_flight[1] == 0
    s.decrement(1)  # No puede bajar de 0
    assert s._in_flight[1] == 0


def test_least_busy_under_1ms():
    s = LeastBusyStrategy()
    deps = _make_deps(10, base_order=1)
    start = time.perf_counter_ns()
    for _ in range(1000):
        s.select(deps, _ctx())
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    assert elapsed_ms < 100


# ── TTFT-aware latency (optim 3) ──────────────────────────────────────────────


def test_latency_strategy_records_ttft():
    """El router graba TTFT (no latencia total) cuando hay streaming; el
    strategy debería ordenar por TTFT."""
    s = LatencyBasedStrategy(maxlen=100, ttl_seconds=300)
    # Dep 1: TTFT pequeño pero latencia total grande
    s.record(1, 50.0)
    # Dep 2: TTFT grande
    s.record(2, 800.0)
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}]
    result = s.select(deps, _ctx())
    assert result["id"] == 1  # el de menor TTFT gana


def test_latency_strategy_prefers_consistent_low_ttft():
    """Mixto: dep 1 con bursting (un TTFT alto + muchos bajos) vs dep 2 estable."""
    s = LatencyBasedStrategy(maxlen=100, ttl_seconds=300)
    for _ in range(10):
        s.record(1, 30.0)
    s.record(1, 2000.0)  # un outlier
    for _ in range(11):
        s.record(2, 80.0)
    deps = [{"id": 1, "order": 1}, {"id": 2, "order": 2}]
    result = s.select(deps, _ctx())
    # Promedio dep1 ≈ (10*30 + 2000)/11 ≈ 209ms; dep2 = 80ms → gana dep 2
    assert result["id"] == 2


import pytest
import db
from unittest.mock import patch, AsyncMock
from services.router import Router, RouterResponse, Deployment
from services.cooldown import CooldownMechanism


# ── RPM/TPM proactive throttle (optim 4) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_rate_limited_excludes_over_rpm(tmp_path):
    """Un deployment con rpm=2 y 2 requests en la ventana actual se excluye,
    PERO si es el único, se mantiene (preferimos probar antes que 503)."""
    from services.cooldown import _bucket
    r = Router()
    r._cooldown = CooldownMechanism(persist_path=tmp_path / "cd.json")
    bucket = _bucket(time.monotonic())
    r._rpm_buckets[1] = {bucket: 2}
    r._rpm_buckets[2] = {bucket: 0}
    deps = [
        Deployment(id=1, model_name="m", provider="p", api_instance_id="i",
                   model_id="m", weight=1.0, rpm=2, tpm=0, max_input_tokens=0, max_parallel_requests=0,
                   order=1, enabled=True),
        Deployment(id=2, model_name="m", provider="p", api_instance_id="i",
                   model_id="m", weight=1.0, rpm=2, tpm=0, max_input_tokens=0, max_parallel_requests=0,
                   order=2, enabled=True),
    ]
    eligible = r._filter_rate_limited(deps)
    # Dep 1 excluido por superar RPM; dep 2 permanece
    assert [d.id for d in eligible] == [2]


@pytest.mark.asyncio
async def test_filter_rate_limited_returns_all_if_all_limited(tmp_path):
    """Si todos superan RPM, se devuelven todos (preferimos 429 del upstream a 503 nuestro)."""
    from services.cooldown import _bucket
    r = Router()
    r._cooldown = CooldownMechanism(persist_path=tmp_path / "cd.json")
    bucket = _bucket(time.monotonic())
    dep = Deployment(
        id=1, model_name="m", provider="p", api_instance_id="i",
        model_id="m", weight=1.0, rpm=2, tpm=0, max_input_tokens=0, max_parallel_requests=0,
        order=1, enabled=True,
    )
    r._rpm_buckets[1] = {bucket: 2}
    eligible = r._filter_rate_limited([dep])
    assert eligible == [dep]  # no se queda sin opciones


@pytest.mark.asyncio
async def test_filter_rate_limited_keeps_within_rpm(tmp_path):
    """Un deployment con rpm=10 y 2 requests en la ventana se mantiene."""
    from services.cooldown import _bucket
    r = Router()
    r._cooldown = CooldownMechanism(persist_path=tmp_path / "cd.json")
    bucket = _bucket(time.monotonic())
    r._rpm_buckets[1] = {bucket: 2}
    dep = Deployment(
        id=1, model_name="m", provider="p", api_instance_id="i",
        model_id="m", weight=1.0, rpm=10, tpm=0, max_input_tokens=0, max_parallel_requests=0,
        order=1, enabled=True,
    )
    eligible = r._filter_rate_limited([dep])
    assert eligible == [dep]


# ── Fallbacks tipados (optim 6) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typed_fallback_only_tries_on_matching_error(tmp_path):
    """Si el error fue RATE_LIMIT, no se prueban context_window_fallbacks."""
    r = Router()
    r._cooldown = CooldownMechanism(persist_path=tmp_path / "cd.json")
    r._settings_cache = {
        "fallbacks": {},
        "default_fallbacks": [],
        "context_window_fallbacks": {"infinity/sonnet": ["big-model"]},
        "content_policy_fallbacks": {},
        "model_group_alias": {},
    }
    # Sin deployments → entra directo a _try_fallbacks
    r._deployments_cache = []
    r._cache_dirty = False
    r._strategy = None
    r._client = None

    # Patch acompletion para trackear qué se prueba
    called = []
    original_acompl = r.acompletion

    async def patched(name, body, stream, visited=None, proxy_type="openai"):
        called.append(name)
        # No hay deployment, simula 503
        from fastapi import HTTPException
        raise HTTPException(503, detail={"message": "fail", "model": name})

    with patch.object(r, "acompletion", side_effect=patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await r._try_fallbacks("infinity/sonnet", {}, False, "orig", ErrorType.RATE_LIMIT)
    # RATE_LIMIT no debería haber probado big-model (context_window_fallback)
    assert "big-model" not in called


@pytest.mark.asyncio
async def test_typed_fallback_tries_context_window_on_match(tmp_path):
    """Si el error fue CONTEXT_WINDOW, SÍ se prueban context_window_fallbacks."""
    r = Router()
    r._cooldown = CooldownMechanism(persist_path=tmp_path / "cd.json")
    r._settings_cache = {
        "fallbacks": {},
        "default_fallbacks": [],
        "context_window_fallbacks": {"infinity/sonnet": ["big-model"]},
        "content_policy_fallbacks": {},
        "model_group_alias": {},
    }
    r._deployments_cache = []
    r._cache_dirty = False

    called = []
    async def patched(name, body, stream, visited=None, proxy_type="openai"):
        called.append(name)
        return RouterResponse(json={"ok": True})

    with patch.object(r, "acompletion", side_effect=patched):
        result = await r._try_fallbacks("infinity/sonnet", {}, False, "orig", ErrorType.CONTEXT_WINDOW_EXCEEDED)
    assert "big-model" in called
    assert result.json == {"ok": True}


from services.provider_handler import ErrorType
import pytest_asyncio
