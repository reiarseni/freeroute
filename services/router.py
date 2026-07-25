"""
Router — orquestador principal LiteLLM-style.
acompletion(model_name, body, stream) → RouterResponse.
Maneja: deployments healthy → routing strategy → retry → error classification →
       cooldown → next deployment → typed fallbacks.
"""

import asyncio
import itertools
import json
import random
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache

import httpx
from fastapi import HTTPException

import db
from services.cooldown import CooldownMechanism, _bucket
from services.provider_handler import (
    ErrorType,
    OAuthReauthRequired,
    get_handler,
    oauth_expires_at,
    oauth_needs_refresh,
    oauth_refresh_token,
    parse_oauth_state,
    set_runtime_providers,
)
from services.routing_strategies import (
    DEFAULT_STRATEGY,
    ROUTING_STRATEGIES,
    RoutingContext,
    _latency_strategy,
    _least_busy_strategy,
    _least_rpm_strategy,
)

# Máximo de reintentos de recuperación cuando el stream se corta a mitad de
# respuesta. Cada reintento reejecuta toda la lógica de deployments + fallbacks;
# 2 pasadas bastan de sobra (el deployment roto ya queda en cooldown).
MAX_STREAM_RECOVERIES = 2

# Reintentos "en-llamada" sobre el MISMO deployment ante un error transitorio
# (saturación/concurrencia, rate limit breve, 5xx) antes de rendirse y pasar al
# siguiente deployment/fallback — equivalente a num_retries de LiteLLM. Target:
# picos de concurrencia ("ResourceExhausted 33/32") que se despejan en ms cuando
# OpenCode lanza varios subagentes en paralelo.
DEFAULT_INCALL_RETRIES = 2
DEFAULT_INCALL_RETRY_BASE = 0.25   # segundos, base del backoff exponencial
INCALL_RETRY_MAX_WAIT = 2.0        # tope de espera por reintento en-llamada

# Semáforo de concurrencia por deployment (estilo max_parallel_requests de LiteLLM).
# Un deployment con max_parallel_requests > 0 nunca tendrá más de N peticiones
# in-flight a la vez: la #N+1 hace COLA hasta que se libera un hueco, previniendo
# el "ResourceExhausted: Worker local total request limit reached". El contador es
# global (threading.Lock) para que el cap valga entre los dos proxies (8787/8788),
# que corren en event loops distintos donde un asyncio.Semaphore no sería válido.
DEFAULT_MAX_PARALLEL_WAIT = 120.0  # s máximos en cola antes de rendirse → fallback
_TRANSIENT_ERRORS = frozenset({
    ErrorType.RATE_LIMIT, ErrorType.SERVER_ERROR, ErrorType.TIMEOUT,
})

# Texto que se emite al cliente antes de reintentar con otro proveedor cuando el
# stream se corta (estrategia "reinicio con marca").
STREAM_RECOVERY_MARKER = "\n\n[infinity: stream cortado, reintentando con otro proveedor…]\n\n"


def _aggregate_openai_sse(raw: bytes) -> dict:
    """Reduce chunks SSE OpenAI-compatible (`data: {...}\\n\\n`) a un único JSON de
    completion no-streaming. Usado cuando `translate_stream` convierte una respuesta
    upstream nativamente streaming (p.ej. Kiro) en SSE, pero el cliente pidió
    `stream: false` — el router necesita darle un solo objeto JSON, no chunks."""
    content = ""
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    completion_id = ""
    model = ""
    usage = None
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        if payload == b"[DONE]" or not payload:
            continue
        try:
            chunk = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            continue
        completion_id = chunk.get("id", completion_id)
        model = chunk.get("model", model)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content += delta["content"]
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {
                    "id": tc.get("id", ""), "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: dict = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    return {
        "id": completion_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
        **({"usage": usage} if usage else {}),
    }


def _recovery_marker_chunk(text: str) -> bytes:
    """Construye un chunk SSE OpenAI-compatible con `text` como delta de contenido.

    Válido para ambos proxies: el 8787 lo reemite tal cual y el traductor 8788 lo
    convierte en un `text_delta` Anthropic, así que la marca aparece como texto en
    el cliente sin cerrar el mensaje en curso.
    """
    chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"


def _inband_stream_error(chunk: bytes) -> str | None:
    """Detecta un error que el upstream mete DENTRO del SSE tras responder 200.

    Varios providers (p.ej. nvidia/nemotron vía kilo) no fallan con un status de
    error: abren el stream y luego emiten `data: {"error": {...}}` con cosas como
    "ResourceExhausted: Worker local total request limit reached (33/32)". Sin esta
    detección el chunk se reenvía tal cual y el cliente lo pinta como texto del
    modelo. Devuelve el payload JSON crudo del error, o None si el chunk es normal.

    Solo cuenta un objeto JSON con `error` de primer nivel: así el texto generado
    por el modelo (que puede hablar de errores) nunca dispara un falso positivo.
    """
    if b"error" not in chunk:
        return None
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("error"):
            return payload.decode("utf-8", "replace")
    return None


@dataclass
class RouterResponse:
    stream: AsyncIterator[bytes] | None = None
    json: dict | None = None
    deployment_used: dict | None = None
    error: Exception | None = None
    _error_type: ErrorType | None = None  # internal — para _try_fallbacks tipados


@dataclass
class Deployment:
    id: int
    model_name: str
    provider: str
    api_instance_id: str
    model_id: str
    weight: float
    rpm: int
    tpm: int
    max_input_tokens: int
    max_parallel_requests: int
    order: int
    enabled: bool

    @classmethod
    def from_row(cls, row: dict) -> "Deployment":
        return cls(
            id=row["id"],
            model_name=row["model_name"],
            provider=row["provider"],
            api_instance_id=row["api_instance_id"],
            model_id=row["model_id"],
            weight=row.get("weight", 1.0),
            rpm=row.get("rpm", 0),
            tpm=row.get("tpm", 0),
            max_input_tokens=row.get("max_input_tokens", 0),
            max_parallel_requests=row.get("max_parallel_requests", 0),
            order=row.get("order", 0),
            enabled=bool(row.get("enabled", 1)),
        )


@lru_cache(maxsize=1)
def _get_tiktoken_encoding():
    # Cachea el encoder BPE (~50KB, ~15-30ms de carga) para no reconstruirlo en
    # cada pre-call check.
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


class ContextWindowExceededError(Exception):
    pass


class ContentPolicyViolationError(Exception):
    pass


class UpstreamStreamInterrupted(Exception):
    def __init__(self, deployment: "Deployment", original: Exception | None = None):
        self.deployment = deployment
        self.original = original
        super().__init__(f"Upstream stream interrupted for deployment {deployment.id}")


class Router:
    def __init__(self):
        self._cache_dirty = True
        self._deployments_cache: list[dict] = []
        self._settings_cache: dict = {}
        self._client: httpx.AsyncClient | None = None
        self._cooldown: CooldownMechanism | None = None
        self._strategy = None
        self._latencies: dict[int, deque] = {}
        self._rpm_buckets: dict[int, dict[int, int]] = {}
        self._tpm_buckets: dict[int, dict[int, int]] = {}
        self._hanging_threshold: float = 0.0
        self._num_incall_retries: int = DEFAULT_INCALL_RETRIES
        self._incall_retry_base: float = DEFAULT_INCALL_RETRY_BASE
        # Limitador de concurrencia por deployment (contador in-flight global).
        self._inflight_lock = threading.Lock()
        self._inflight: dict[int, int] = {}
        self._max_parallel_wait: float = DEFAULT_MAX_PARALLEL_WAIT
        # Lock por api_instance_id para serializar el refresh perezoso de tokens
        # oauth_device (evita refrescos concurrentes duplicados).
        self._oauth_locks: dict[str, asyncio.Lock] = {}
        # Peticiones de cliente en curso (una entrada por request externa, no por
        # intento de deployment/fallback), para mostrar estado "en proceso" en /logs
        # antes de que exista una fila en request_logs.
        self._active_lock = threading.Lock()
        self._active: dict[int, dict] = {}
        self._active_seq = itertools.count(1)

    def _register_active(self, model_name: str, proxy_type: str, body: dict) -> int:
        active_id = next(self._active_seq)
        with self._active_lock:
            self._active[active_id] = {
                "id": active_id,
                "model_name": model_name,
                "proxy_type": proxy_type,
                "original_model": body.get("model", model_name),
                "started_at": time.time(),
            }
        return active_id

    def _unregister_active(self, active_id: int | None):
        if active_id is None:
            return
        with self._active_lock:
            self._active.pop(active_id, None)

    def get_active_requests(self) -> list[dict]:
        with self._active_lock:
            return list(self._active.values())

    async def _ensure_init(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
                # read=60s: un upstream que no manda un chunk en 60s no va a
                # mandar nada útil (NVIDIA NIM con deepseek-v4-pro se queda
                # callado tras accept() y devoraba read=120s por deployment,
                # sumando ~302s en cadenas de fallback). Subido de 30s a 60s
                # porque Z.AI GLM-Flash (gratis) puede tardar 45s+ en no-stream
                # por rate-limit + reasoning_content. El hanging_threshold
                # cubre el TTFT; este read cubre el resto del stream/body.
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=10.0),
            )
        if self._cache_dirty:
            await self._reload_cache()

    async def _reload_cache(self):
        self._deployments_cache = await db.get_deployments()
        self._settings_cache = await db.get_router_settings()
        # Poblar el registry de handlers desde la tabla providers (data-driven).
        set_runtime_providers(await db.get_providers())

        allowed_fails_raw = self._settings_cache.get("allowed_fails")
        allowed_fails = None
        if allowed_fails_raw:
            allowed_fails = {}
            for k, v in allowed_fails_raw.items():
                try:
                    allowed_fails[ErrorType(k)] = int(v)
                except (ValueError, KeyError):
                    allowed_fails[ErrorType.UNKNOWN] = int(v)

        cooldown_times_raw = self._settings_cache.get("cooldown_times")
        cooldown_time: float | dict[ErrorType, float] | None = None
        if cooldown_times_raw:
            cooldown_time = {}
            for k, v in cooldown_times_raw.items():
                try:
                    cooldown_time[ErrorType(k)] = float(v)
                except (ValueError, KeyError):
                    pass
            if not cooldown_time:
                cooldown_time = None
        else:
            cooldown_time = self._settings_cache.get("cooldown_time", 60.0)

        self._cooldown = CooldownMechanism(
            allowed_fails=allowed_fails,
            cooldown_time=cooldown_time,
        )

        self._hanging_threshold = float(self._settings_cache.get("hanging_threshold", 0.0) or 0.0)
        self._num_incall_retries = int(
            self._settings_cache.get("num_retries", DEFAULT_INCALL_RETRIES)
        )
        self._incall_retry_base = float(
            self._settings_cache.get("incall_retry_base_delay", DEFAULT_INCALL_RETRY_BASE)
        )
        self._max_parallel_wait = float(
            self._settings_cache.get("max_parallel_requests_timeout", DEFAULT_MAX_PARALLEL_WAIT)
        )

        strategy_name = self._settings_cache.get("routing_strategy", DEFAULT_STRATEGY)
        self._strategy = ROUTING_STRATEGIES.get(strategy_name, ROUTING_STRATEGIES[DEFAULT_STRATEGY])

        self._cache_dirty = False

    def invalidate_cache(self):
        self._cache_dirty = True

    async def acompletion(
        self, model_name: str, body: dict, stream: bool = False,
        _visited: set[str] | None = None, proxy_type: str = "openai",
    ) -> RouterResponse:
        # "Activa" solo se registra en la llamada más externa (_visited is None):
        # las llamadas recursivas de _try_fallbacks son intentos internos de la
        # misma petición de cliente, no peticiones nuevas.
        is_outer = _visited is None
        active_id = self._register_active(model_name, proxy_type, body) if is_outer else None
        try:
            result = await self._acompletion(model_name, body, stream, _visited, proxy_type)
            # Recuperación de stream cortado a mitad de respuesta: solo en la llamada
            # más externa (_visited is None, las de fallback pasan un set). Envuelve el
            # stream para que, si el upstream rompe la conexión a mitad, se reintente
            # con otro deployment/fallback reusando la lógica de reintentos existente
            # (el deployment roto ya quedó en cooldown en _try_deployment).
            if stream and is_outer and result.error is None and result.stream is not None:
                result.stream = self._resilient_stream(result.stream, model_name, body, proxy_type)
            return result
        finally:
            self._unregister_active(active_id)

    async def _acompletion(
        self, model_name: str, body: dict, stream: bool = False,
        _visited: set[str] | None = None, proxy_type: str = "openai",
    ) -> RouterResponse:
        await self._ensure_init()

        # Pedir usage en streaming a todos los upstream OpenAI-compatible.
        # Sin esto los proveedores no emiten tokens en el stream, y ni el proxy
        # OpenAI (8787, passthrough) ni el Anthropic (8788, traducción) pueden
        # mostrar el contador de tokens en vivo. Se inyecta aquí una sola vez
        # para que ambas variantes sean coherentes.
        if stream and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}

        aliases = self._settings_cache.get("model_group_alias", {})
        resolved_name = aliases.get(model_name, model_name)

        # Corta ciclos en la cadena de fallbacks (p.ej. default_fallbacks que
        # apunta a un model_name ya intentado, directa o indirectamente vía
        # alias): sin esto, un fallback que se refiere a sí mismo (o a otro ya
        # visitado) recursiona sin límite hasta RecursionError.
        visited = _visited if _visited is not None else set()
        visited.add(resolved_name)

        all_deployments = [
            Deployment.from_row(d)
            for d in self._deployments_cache
            if d["model_name"] == resolved_name and d.get("enabled", 1)
        ]

        if not all_deployments:
            return await self._try_fallbacks(resolved_name, body, stream, model_name, None, visited, proxy_type)

        healthy = [d for d in all_deployments if not self._cooldown.is_cooling_down(d.id)]

        if not healthy:
            return await self._try_fallbacks(resolved_name, body, stream, model_name, None, visited, proxy_type)

        ctx = RoutingContext(
            model_name=resolved_name,
            original_model=model_name,
            body=body,
            stream=stream,
        )

        # Seleccionar deployment principal con throttle RPM/TPM
        eligible = self._filter_rate_limited(healthy)
        if not eligible:
            return await self._try_fallbacks(resolved_name, body, stream, model_name, None, visited, proxy_type)

        if len(eligible) == 1:
            deployment = eligible[0]
        else:
            least_rpm_models = self._settings_cache.get("least_rpm_models", {})
            if least_rpm_models.get(resolved_name):
                ctx.args["rpm_counts"] = self._current_rpm_counts(eligible)
                deployment = _least_rpm_strategy.select(
                    [self._deployment_to_dict(d) for d in eligible], ctx
                )
            else:
                deployment = self._strategy.select(
                    [self._deployment_to_dict(d) for d in eligible], ctx
                )
            deployment = next(d for d in eligible if d.id == deployment["id"])

        result = await self._try_deployment(deployment, body, stream, proxy_type)
        if result is not None and result.error is None:
            return result
        last_error_type = result._error_type if result is not None else None

        for d in eligible:
            if d.id == deployment.id:
                continue
            result = await self._try_deployment(d, body, stream, proxy_type)
            if result is not None and result.error is None:
                return result
            if result is not None and result._error_type is not None:
                last_error_type = result._error_type

        return await self._try_fallbacks(resolved_name, body, stream, model_name, last_error_type, visited, proxy_type)

    def _try_acquire_slot(self, dep_id: int, limit: int) -> bool:
        """Intenta ocupar un hueco de concurrencia sin bloquear. True si lo consigue."""
        with self._inflight_lock:
            cur = self._inflight.get(dep_id, 0)
            if cur >= limit:
                return False
            self._inflight[dep_id] = cur + 1
            return True

    def _release_slot(self, dep_id: int) -> None:
        """Libera un hueco de concurrencia previamente ocupado."""
        with self._inflight_lock:
            cur = self._inflight.get(dep_id, 0)
            if cur <= 1:
                self._inflight.pop(dep_id, None)
            else:
                self._inflight[dep_id] = cur - 1

    async def _acquire_slot(self, dep_id: int, limit: int, timeout: float) -> bool:
        """Espera un hueco de concurrencia (poll asíncrono, agnóstico al event loop).

        Devuelve True al ocuparlo, False si se agota `timeout` sin lograrlo. El poll
        con backoff evita busy-wait; el lock solo protege ops de dict (µs), así que no
        bloquea el loop de forma apreciable."""
        if self._try_acquire_slot(dep_id, limit):
            return True
        deadline = time.monotonic() + timeout
        delay = 0.01
        while True:
            await asyncio.sleep(delay)
            if self._try_acquire_slot(dep_id, limit):
                return True
            if time.monotonic() >= deadline:
                return False
            delay = min(0.1, delay * 1.5)

    def _retry_backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Backoff exponencial con jitter para los reintentos en-llamada, acotado a
        INCALL_RETRY_MAX_WAIT. Respeta un retry_after corto si el upstream lo indica."""
        base = self._incall_retry_base * (2 ** attempt)
        delay = min(INCALL_RETRY_MAX_WAIT, base + random.uniform(0, self._incall_retry_base))
        if retry_after is not None:
            delay = min(INCALL_RETRY_MAX_WAIT, max(delay, retry_after))
        return delay

    async def _try_deployment(
        self, deployment: Deployment, body: dict, stream: bool, proxy_type: str = "openai"
    ) -> RouterResponse | None:
        """Intenta un deployment con reintentos en-llamada ante errores transitorios.

        Ante saturación/concurrencia ("ResourceExhausted 33/32"), rate limit breve o
        5xx, reintenta la MISMA petición con backoff exponencial+jitter (estilo
        num_retries de LiteLLM) antes de rendirse. Solo entonces marca cooldown y deja
        que el caller pruebe el siguiente deployment/fallback. Devuelve RouterResponse
        si OK, None si falló."""
        instance = await db.get_instance(deployment.api_instance_id)
        if not instance:
            return None

        handler = get_handler(instance["provider"])
        if handler.auth_type == "oauth_device":
            instance = await self._ensure_oauth_fresh(instance, handler)
            if instance is None:
                # needs_reauth: estado persistente, no transitorio — se excluye
                # del enrutado sin pasar por el cooldown genérico de AUTH_ERROR.
                return None

        is_single_deployment = self._is_single_deployment(deployment.model_name)

        enable_pre_call = self._settings_cache.get("enable_pre_call_checks", False)
        if enable_pre_call and deployment.max_input_tokens > 0:
            try:
                self._check_context_window(body, deployment.max_input_tokens)
            except ContextWindowExceededError:
                await self._cooldown.mark_failure(
                    deployment.id, ErrorType.CONTEXT_WINDOW_EXCEEDED,
                    is_single_deployment=is_single_deployment,
                )
                return None

        now = time.monotonic()
        b = _bucket(now)
        self._rpm_buckets.setdefault(deployment.id, {})[b] = \
            self._rpm_buckets.setdefault(deployment.id, {}).get(b, 0) + 1
        self._tpm_buckets.setdefault(deployment.id, {})[b] = \
            self._tpm_buckets.setdefault(deployment.id, {}).get(b, 0) + self._estimate_input_tokens(body)
        _least_busy_strategy.increment(deployment.id)

        for store in (self._rpm_buckets, self._tpm_buckets):
            for dep_id in list(store.keys()):
                store[dep_id] = {bk: cnt for bk, cnt in store[dep_id].items() if bk >= b - 1}
                if not store[dep_id]:
                    del store[dep_id]

        error_type: ErrorType | None = None
        error_status: int = 503

        async def _log_failed_attempt(latency_ms: float, err: ErrorType | None) -> None:
            # Deja visible en /logs cada intento de deployment que falla dentro de
            # la cadena de fallback (antes eran invisibles: solo se logueaban los
            # éxitos y los errores HTTP del upstream, no los timeouts/cortes
            # detectados por el propio proxy).
            await db.insert_log({
                "proxy_type": proxy_type,
                "chain_id": deployment.model_name,
                "original_model": body.get("model", ""),
                "api_instance_id": deployment.api_instance_id,
                "model_id": deployment.model_id,
                "status_code": 0,
                "latency_ms": int(latency_ms),
                "error_type": err.value if err else None,
            })

        # Limitador de concurrencia: si el deployment tiene tope, ocupa un hueco
        # (esperando en cola si hace falta). El hueco se libera al terminar la
        # request; para streams se transfiere al generador (se libera al cerrarse).
        limit = deployment.max_parallel_requests
        slot_acquired = False
        slot_transferred = False

        try:
            if limit > 0:
                if not await self._acquire_slot(deployment.id, limit, self._max_parallel_wait):
                    # Cola saturada demasiado tiempo: no es un fallo del deployment
                    # (no cooldown) — devolvemos error para que el caller pruebe otro.
                    error_type = ErrorType.RATE_LIMIT
                    return RouterResponse(
                        error=HTTPException(429, detail=str(error_type)),
                        _error_type=error_type,
                    )
                slot_acquired = True

            # Hook opcional de traducción (p.ej. kind=kiro reescribe el body al
            # conversationState de Kiro). Pass-through para el resto de providers.
            body = await handler.translate_request(body)

            # El provider del deployment es la única fuente de verdad: el model_id
            # se envía completo al upstream (muchos providers, p.ej. kilo, usan
            # model_ids con '/' como "nvidia/nemotron-…" que NO deben interpretarse
            # como prefijo de otro provider).
            prepared = await handler.pre_call(
                {"model_id": deployment.model_id, "id": deployment.id}, instance, body
            )

            # Reintentos en-llamada: el body ya está streameado al cliente solo tras
            # el `return` de éxito, así que reintentar aquí (errores pre-stream) es
            # seguro. Los cortes DESPUÉS del 200 los cubre _resilient_stream.
            for attempt in range(self._num_incall_retries + 1):
                error_type = None
                retry_after: float | None = None
                start_ns = time.perf_counter_ns()
                try:
                    # httpx>=0.28 eliminó el kwarg `timeout` de AsyncClient.send();
                    # el timeout por-request ahora se fija en build_request().
                    request = self._client.build_request(
                        "POST", prepared.url, json=prepared.body, headers=prepared.headers,
                        # Lectura ligada a hanging_threshold (configurable en Router
                        # Settings): si el upstream deja de mandar bytes —sea antes
                        # del primer chunk o a mitad de un stream ya iniciado— más de
                        # ese tiempo, httpx corta con ReadTimeout y se prueba otro
                        # deployment/fallback en vez de esperar indefinidamente.
                        timeout=httpx.Timeout(
                            connect=5.0,
                            read=self._hanging_threshold if self._hanging_threshold > 0 else 60.0,
                            write=30.0,
                            pool=10.0,
                        ),
                    )
                    resp = await self._client.send(request, stream=True)
                except Exception:
                    # Fallo de conexión — suele ser saturación transitoria: reintentar.
                    error_type = ErrorType.TIMEOUT
                    if attempt < self._num_incall_retries:
                        await asyncio.sleep(self._retry_backoff(attempt))
                        continue
                    await self._cooldown.mark_failure(
                        deployment.id, error_type, is_single_deployment=is_single_deployment,
                    )
                    await _log_failed_attempt(
                        (time.perf_counter_ns() - start_ns) / 1_000_000, error_type,
                    )
                    return None

                if resp.status_code == 200:
                    if stream:
                        # TTFT para streaming con hanging detection
                        ttft_start = time.perf_counter_ns()
                        ait = handler.translate_stream(resp.aiter_bytes())
                        ttft_ms: float | None = None

                        async def timed_stream():
                            nonlocal ttft_ms
                            first = True
                            async for chunk in ait:
                                if first:
                                    ttft_ms = (time.perf_counter_ns() - ttft_start) / 1_000_000
                                    first = False
                                yield chunk

                        gen = timed_stream()
                        try:
                            # Consume el primer chunk con timeout (hanging detection)
                            first_chunk = await asyncio.wait_for(
                                gen.__anext__(),
                                timeout=self._hanging_threshold if self._hanging_threshold > 0 else None,
                            )
                        except (asyncio.TimeoutError, StopAsyncIteration):
                            await self._cooldown.mark_failure(
                                deployment.id, ErrorType.TIMEOUT, is_single_deployment=is_single_deployment,
                            )
                            await _log_failed_attempt(
                                (time.perf_counter_ns() - start_ns) / 1_000_000, ErrorType.TIMEOUT,
                            )
                            return None

                        # Error en banda en el PRIMER chunk: nada ha salido aún hacia
                        # el cliente, así que se trata como un fallo pre-stream —
                        # cooldown + None para que el caller pruebe el siguiente
                        # deployment/fallback sin que el usuario vea el mensaje.
                        inband = _inband_stream_error(first_chunk)
                        if inband is not None:
                            await resp.aclose()
                            error_type = handler.parse_error(200, inband).error_type
                            await self._cooldown.mark_failure(
                                deployment.id, error_type, is_single_deployment=is_single_deployment,
                            )
                            await _log_failed_attempt(
                                (time.perf_counter_ns() - start_ns) / 1_000_000, error_type,
                            )
                            return None

                        # El primero ya se consumió: encadenamos first_chunk con el resto.
                        # El hueco de concurrencia se libera aquí (finally), al cerrarse
                        # el stream — sea por fin normal, corte o desconexión del cliente.
                        async def chained():
                            try:
                                yield first_chunk
                                try:
                                    async for c in gen:
                                        # Error en banda a mitad de respuesta: NO se
                                        # reenvía al cliente. Se corta como si el
                                        # upstream hubiera roto la conexión para que
                                        # _resilient_stream retome con otro deployment.
                                        inband_mid = _inband_stream_error(c)
                                        if inband_mid is not None:
                                            await self._cooldown.mark_failure(
                                                deployment.id,
                                                handler.parse_error(200, inband_mid).error_type,
                                                is_single_deployment=is_single_deployment,
                                            )
                                            raise UpstreamStreamInterrupted(
                                                deployment=deployment,
                                                original=RuntimeError(inband_mid),
                                            )
                                        yield c
                                except UpstreamStreamInterrupted:
                                    raise
                                except Exception as exc:
                                    await self._cooldown.mark_failure(
                                        deployment.id, ErrorType.TIMEOUT, is_single_deployment=is_single_deployment,
                                    )
                                    await db.insert_log({
                                        "proxy_type": proxy_type,
                                        "chain_id": deployment.model_name,
                                        "original_model": body.get("model", ""),
                                        "api_instance_id": deployment.api_instance_id,
                                        "model_id": deployment.model_id,
                                        "status_code": 0,
                                        "latency_ms": int((time.perf_counter_ns() - start_ns) / 1_000_000),
                                        "error_type": ErrorType.TIMEOUT.value,
                                    })
                                    raise UpstreamStreamInterrupted(deployment=deployment, original=exc) from exc
                            finally:
                                if limit > 0:
                                    self._release_slot(deployment.id)

                        latency_ms = ttft_ms if ttft_ms is not None else \
                            (time.perf_counter_ns() - start_ns) / 1_000_000

                        await self._cooldown.mark_success(deployment.id)
                        self._latencies.setdefault(deployment.id, deque(maxlen=100)).append(
                            (time.monotonic(), latency_ms)
                        )
                        _latency_strategy.record(deployment.id, latency_ms)

                        await db.insert_log({
                            "proxy_type": proxy_type,
                            "chain_id": deployment.model_name,
                            "original_model": body.get("model", ""),
                            "api_instance_id": deployment.api_instance_id,
                            "model_id": deployment.model_id,
                            "status_code": 200,
                            "latency_ms": int(latency_ms),
                        })

                        # El hueco pasa a ser propiedad del stream (lo libera chained).
                        slot_transferred = True
                        return RouterResponse(
                            stream=chained(),
                            deployment_used=self._deployment_to_dict(deployment),
                        )
                    else:
                        try:
                            if getattr(handler, "response_is_stream", False):
                                # El upstream (p.ej. Kiro) siempre responde en su propio
                                # formato streaming, incluso a peticiones no-streaming del
                                # cliente: se traduce a SSE con translate_stream y se
                                # reduce a un único JSON de completion.
                                raw = await resp.aread()

                                async def _one_chunk(_raw=raw):
                                    yield _raw

                                translated = b"".join(
                                    [c async for c in handler.translate_stream(_one_chunk())]
                                )
                                data = _aggregate_openai_sse(translated)
                            else:
                                await resp.aread()
                                data = resp.json()
                        except Exception:
                            await self._cooldown.mark_failure(
                                deployment.id, ErrorType.TIMEOUT, is_single_deployment=is_single_deployment,
                            )
                            await _log_failed_attempt(
                                (time.perf_counter_ns() - start_ns) / 1_000_000, ErrorType.TIMEOUT,
                            )
                            return None
                        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

                        await self._cooldown.mark_success(deployment.id)
                        self._latencies.setdefault(deployment.id, deque(maxlen=100)).append(
                            (time.monotonic(), latency_ms)
                        )
                        _latency_strategy.record(deployment.id, latency_ms)

                        await db.insert_log({
                            "proxy_type": proxy_type,
                            "chain_id": deployment.model_name,
                            "original_model": body.get("model", ""),
                            "api_instance_id": deployment.api_instance_id,
                            "model_id": deployment.model_id,
                            "status_code": 200,
                            "latency_ms": int(latency_ms),
                        })

                        return RouterResponse(
                            json=data,
                            deployment_used=self._deployment_to_dict(deployment),
                        )

                # Error HTTP — clasificar
                await resp.aread()
                error_body = resp.text
                error_status = resp.status_code
                classification = handler.parse_error(resp.status_code, error_body)
                error_type = classification.error_type
                if error_type == ErrorType.RATE_LIMIT:
                    retry_after = self._parse_retry_after(resp.headers.get("retry-after"))

                await db.insert_log({
                    "proxy_type": proxy_type,
                    "chain_id": deployment.model_name,
                    "original_model": body.get("model", ""),
                    "api_instance_id": deployment.api_instance_id,
                    "model_id": deployment.model_id,
                    "status_code": resp.status_code,
                    "latency_ms": int((time.perf_counter_ns() - start_ns) / 1_000_000),
                    "error_type": error_type.value if error_type else None,
                })

                # Reintento en-llamada para errores transitorios (concurrencia / 5xx /
                # rate limit breve). Un retry_after largo NO se espera aquí: se deja al
                # cooldown+fallback. UNKNOWN tampoco se reintenta (mismo payload fallará).
                if (
                    error_type in _TRANSIENT_ERRORS
                    and attempt < self._num_incall_retries
                    and (retry_after is None or retry_after <= INCALL_RETRY_MAX_WAIT)
                ):
                    await asyncio.sleep(self._retry_backoff(attempt, retry_after))
                    continue

                # Agotados los reintentos → cooldown (salvo UNKNOWN, que no ayuda y
                # penaliza otras requests que sí funcionarían) y salir del bucle.
                if error_type != ErrorType.UNKNOWN:
                    await self._cooldown.mark_failure(
                        deployment.id, error_type,
                        is_single_deployment=is_single_deployment,
                        retry_after=retry_after,
                    )
                break

        finally:
            _least_busy_strategy.decrement(deployment.id)
            # Libera el hueco de concurrencia salvo que se haya transferido al stream
            # (que lo libera al cerrarse en su propio finally).
            if slot_acquired and not slot_transferred:
                self._release_slot(deployment.id)

        if error_type is not None:
            return RouterResponse(
                error=HTTPException(error_status, detail=str(error_type)),
                _error_type=error_type,
            )
        return None

    async def _ensure_oauth_fresh(self, instance: dict, handler) -> dict | None:
        """Refresco perezoso del access_token de una instancia oauth_device.

        Si el token vigente no expira dentro del margen de seguridad, devuelve
        la instancia tal cual (sin llamada de red). Si toca refrescar, serializa
        con un lock por instancia y relee el estado tras adquirirlo (por si otro
        request concurrente ya refrescó mientras se esperaba). Devuelve None si
        la instancia está (o queda) en needs_reauth — el caller la excluye del
        enrutado sin pasar por el cooldown genérico.
        """
        state = parse_oauth_state(instance)
        if state.get("status") in ("needs_reauth", "needs_profile_arn"):
            return None
        if not oauth_needs_refresh(state):
            return instance

        instance_id = instance["id"]
        lock = self._oauth_locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            fresh = await db.get_instance(instance_id)
            if not fresh:
                return None
            state = parse_oauth_state(fresh)
            if state.get("status") in ("needs_reauth", "needs_profile_arn"):
                return None
            if not oauth_needs_refresh(state):
                return fresh

            await self._ensure_init()
            try:
                token_resp = await oauth_refresh_token(
                    self._client, handler.base_url, handler.kind,
                    state.get("client_id", ""), state.get("client_secret", ""),
                    state.get("refresh_token", ""),
                )
            except OAuthReauthRequired:
                state["status"] = "needs_reauth"
                await db.set_oauth_state(instance_id, state)
                return None

            new_state = {
                **state,
                "access_token": token_resp.get("accessToken", token_resp.get("access_token", "")),
                "refresh_token": token_resp.get(
                    "refreshToken", token_resp.get("refresh_token", state.get("refresh_token", ""))
                ),
                "expires_at": oauth_expires_at(token_resp),
                "status": "active",
            }
            await db.set_oauth_state(instance_id, new_state)
            fresh["oauth_state"] = json.dumps(new_state)
            return fresh

    def _is_single_deployment(self, model_name: str) -> bool:
        """True si `model_name` tiene exactamente 1 deployment habilitado en la cache."""
        count = sum(
            1 for d in self._deployments_cache
            if d["model_name"] == model_name and d.get("enabled", 1)
        )
        return count == 1

    @staticmethod
    def _parse_retry_after(raw: str | None) -> float | None:
        """Parsea el header Retry-After (segundos) si es numérico y está en [0, 60]."""
        if not raw:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if 0 <= value <= 60:
            return value
        return None

    def _current_rpm_counts(self, deployments: list[Deployment]) -> dict[int, int]:
        """RPM consumido por deployment en el bucket actual + el anterior — mismo
        cálculo que usa `_filter_rate_limited` para el throttle, reutilizado aquí
        para elegir el deployment menos cargado."""
        bucket = _bucket(time.monotonic())
        return {
            d.id: sum(self._rpm_buckets.get(d.id, {}).get(b, 0) for b in (bucket - 1, bucket))
            for d in deployments
        }

    def _filter_rate_limited(self, deployments: list[Deployment]) -> list[Deployment]:
        """Filtro proactivo: excluye deployments que superen RPM/TPM en la ventana actual.

        Si TODOS están sobre el límite, devuelve la lista original (preferimos
        probar y dejar que el upstream devuelva 429 antes que bloquear todo).
        """
        bucket = _bucket(time.monotonic())
        eligible = []
        for d in deployments:
            if d.rpm > 0:
                rpm_now = sum(
                    self._rpm_buckets.get(d.id, {}).get(b, 0)
                    for b in (bucket - 1, bucket)
                )
                if rpm_now >= d.rpm:
                    continue
            if d.tpm > 0:
                tpm_now = sum(
                    self._tpm_buckets.get(d.id, {}).get(b, 0)
                    for b in (bucket - 1, bucket)
                )
                if tpm_now >= d.tpm:
                    continue
            eligible.append(d)
        return eligible if eligible else deployments

    @staticmethod
    def _estimate_input_tokens(body: dict) -> int:
        # Heurística barata en hot path: ~4 chars/token sobre el texto de los
        # mensajes, sin serializar todo el body con json.dumps (que se ejecutaba
        # en cada request solo para el bucketing de TPM).
        total = 0
        for m in body.get("messages", []):
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(block.get("text") or "")
                    elif isinstance(block, str):
                        total += len(block)
        return max(1, total // 4)

    async def _try_fallbacks(
        self, model_name: str, body: dict, stream: bool, original_model: str,
        error_type: ErrorType | None = None, visited: set[str] | None = None,
        proxy_type: str = "openai",
    ) -> RouterResponse:
        fallbacks = self._settings_cache.get("fallbacks", {})
        default_fallbacks = self._settings_cache.get("default_fallbacks", [])
        if visited is None:
            visited = {model_name}

        # Fallbacks específicos
        for fallback_name in fallbacks.get(model_name, []):
            if fallback_name in visited:
                continue
            result = await self.acompletion(fallback_name, body, stream, visited, proxy_type)
            if result.error is None:
                return result

        # Default fallbacks
        for fallback_name in default_fallbacks:
            if fallback_name in visited:
                continue
            result = await self.acompletion(fallback_name, body, stream, visited, proxy_type)
            if result.error is None:
                return result

        # Fallbacks tipados: solo si el error que disparó el fallback coincice
        if error_type == ErrorType.CONTEXT_WINDOW_EXCEEDED:
            ctx_fallbacks = self._settings_cache.get("context_window_fallbacks", {})
            for fallback_name in ctx_fallbacks.get(model_name, []):
                if fallback_name in visited:
                    continue
                result = await self.acompletion(fallback_name, body, stream, visited, proxy_type)
                if result.error is None:
                    return result

        if error_type == ErrorType.CONTENT_POLICY_VIOLATION:
            cp_fallbacks = self._settings_cache.get("content_policy_fallbacks", {})
            for fallback_name in cp_fallbacks.get(model_name, []):
                if fallback_name in visited:
                    continue
                result = await self.acompletion(fallback_name, body, stream, visited, proxy_type)
                if result.error is None:
                    return result

        raise HTTPException(
            503,
            detail={
                "message": f"Todos los proveedores de {model_name} fallaron",
                "model": original_model,
            },
        )

    def _check_context_window(self, body: dict, max_tokens: int):
        messages = body.get("messages", [])
        if not messages:
            return
        estimated_tokens = self._count_tokens(messages, body)
        if estimated_tokens > max_tokens:
            raise ContextWindowExceededError(
                f"Input estimado ({estimated_tokens} tokens) excede max_input_tokens ({max_tokens})"
            )

    @staticmethod
    def _count_tokens(messages: list, body: dict) -> int:
        """Token count con tiktoken (OpenAI-shape) + heurística Anthropic-shape.

        Detecta el formato por presencia de claves exclusivas de Anthropic en
        los mensajes (content como lista de bloques, tool_use, server_tool_use).
        Si es Anthropic-shape, aplica un factor 1.3x sobre el conteo OpenAI.

        Si tiktoken no está instalado o falla, hace fallback a len(text)//4.
        """
        try:
            enc = _get_tiktoken_encoding()
        except Exception:
            # Fallback al método simple si tiktoken no disponible
            text = json.dumps(messages)
            return max(1, len(text) // 4)

        text = json.dumps(messages)
        token_count = len(enc.encode(text))

        # Detectar si es Anthropic-shape (mensajes con content como lista de bloques
        # o presencia de tool_use / server_tool_use / content_block_start, etc.)
        is_anthropic = False
        for m in messages:
            if isinstance(m.get("content"), list):
                is_anthropic = True
                break
            if any(k in m for k in ("tool_use", "server_tool_use", "tool_result")):
                is_anthropic = True
                break

        # Anthropic tiene overhead de formato mayor (~30% más tokens que
        # la estimación tiktoken sobre JSON).
        if is_anthropic:
            return int(token_count * 1.3)

        return token_count

    @staticmethod
    def _deployment_to_dict(d: Deployment) -> dict:
        return {
            "id": d.id,
            "model_name": d.model_name,
            "provider": d.provider,
            "api_instance_id": d.api_instance_id,
            "model_id": d.model_id,
            "weight": d.weight,
            "rpm": d.rpm,
            "tpm": d.tpm,
            "max_input_tokens": d.max_input_tokens,
            "max_parallel_requests": d.max_parallel_requests,
            "order": d.order,
            "enabled": d.enabled,
        }

    async def _resilient_stream(
        self, stream: AsyncIterator[bytes], model_name: str, body: dict, proxy_type: str,
    ) -> AsyncIterator[bytes]:
        """Envuelve el stream de un deployment y lo hace tolerante a cortes a mitad.

        Reenvía los chunks tal cual. Si el upstream rompe la conexión a mitad de
        respuesta (`UpstreamStreamInterrupted`), emite una marca visible y reintenta
        con `_acompletion` (que reejecuta deployments + fallbacks; el roto ya está en
        cooldown). Agotados los reintentos —o sin proveedor disponible— relanza la
        excepción para que los proxies emitan el error como hasta ahora.
        """
        recoveries = 0
        while True:
            try:
                async for chunk in stream:
                    yield chunk
                return
            except UpstreamStreamInterrupted as exc:
                if recoveries >= MAX_STREAM_RECOVERIES:
                    raise
                recoveries += 1
                yield _recovery_marker_chunk(STREAM_RECOVERY_MARKER)
                try:
                    result = await self._acompletion(
                        model_name, body, stream=True, _visited=None, proxy_type=proxy_type,
                    )
                except HTTPException:
                    raise exc from None
                if result.error is not None or result.stream is None:
                    raise exc from None
                stream = result.stream

    async def aclose(self):
        if self._client:
            await self._client.aclose()
            self._client = None


router = Router()
