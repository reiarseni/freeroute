# HALLAZGOS — Infinity Provisioner v4 (Router LiteLLM-style)

**Análisis inicial:** 2025-07-21 · **Resolución y re-verificación:** 2026-07-22
**Metodología:** análisis estático + verificación contra código real + tests unitarios (74 pasan).

## Estado

Todos los hallazgos reales han sido **resueltos y verificados** esta sesión. Los ítems de "arquitectura
futura" se reclasificaron tras verificar el código actual: varios eran falsos positivos o ya estaban
cubiertos por la infraestructura data-driven de la v4.

| # | Hallazgo | Estado |
|---|----------|--------|
| 1 | UTF-8 chunk boundary en streaming | ✅ Resuelto |
| 2 | `json.dumps` en hot path (`_estimate_input_tokens`) | ✅ Resuelto |
| 3 | `tiktoken.get_encoding()` sin cache | ✅ Resuelto |
| 4 | `_in_flight` con doble fuente de verdad | ✅ Resuelto |
| 6 | Misrouteo por prefijo de `model_id` vs `provider` | ✅ Resuelto (era bug real, no solo validación) |
| 7 | `router_settings` sin validación de tipos | ⚠️ Mitigado (parsing defensivo en `_reload_cache`) |
| 8 | `/api/chains` deprecado visible en OpenAPI | ✅ Resuelto |
| 9 | `index` de tool_calls de Gemini en proxy Anthropic | ✅ Resuelto |
| 10 | `resolve_claude_model` hardcoded | ❌ Falso positivo (ya configurable) |
| 11 | Benchmark test faltante | ❌ Falso positivo (spec eliminado) |
| 12 | Endpoints sin tags OpenAPI | ✅ Resuelto |
| 13 | Config hardcoded (puertos, DB_PATH) | ✅ Resuelto |

---

## Resueltos esta sesión

### 1. UTF-8 chunk boundary en streaming
`aiter_bytes()` devuelve chunks no alineados a UTF-8; `bytes.decode(errors="replace")` por chunk
insertaba `�` al partir un carácter multibyte (emoji, ñ, CJK). **Fix:** decoder incremental
(`codecs.getincrementaldecoder("utf-8")`) en `routers/openai_proxy.py` **y** en
`services/translators.py:_iter_sse_lines` (el informe original solo mencionaba el primero).

### 2. `json.dumps` en hot path
`_estimate_input_tokens` serializaba todo el body en cada request solo para el bucketing de TPM.
**Fix:** heurística que suma la longitud del texto de los mensajes (str y bloques). Además había una
**segunda definición idéntica** de la función que sobrescribía la primera (código muerto) → eliminada.
`services/router.py`.

### 3. `tiktoken.get_encoding()` sin cache
Reconstruía el vocabulario BPE en cada pre-call check. **Fix:** `@lru_cache(maxsize=1)` a nivel módulo
(`_get_tiktoken_encoding`). `services/router.py`.

### 4. `_in_flight` con doble fuente de verdad
El `Router` mantenía su propio `_in_flight` y lo **copiaba a mano** a `LeastBusyStrategy`, que podía
desincronizarse tras errores. **Fix:** una sola fuente — el router usa `LeastBusyStrategy.increment/
decrement` (métodos que estaban **muertos**); `router_stats` lee del mismo dict.
`services/router.py`, `routers/router_stats.py`, `services/routing_strategies.py`.

### 6. Misrouteo por prefijo de `model_id` vs `provider`
**Era un bug real, no solo un gap de validación.** `_try_deployment` pasaba el `model_id` por
`parse_model_id_prefix`; si el primer segmento coincidía con un provider conocido distinto al del
deployment, **conmutaba el handler a ese provider**. Confirmado en datos vivos: el deployment
habilitado de **kilo** con `model_id="nvidia/nemotron-3-super-120b-a12b:free"` se enviaba a la base de
**NVIDIA** con la instancia de kilo. **Fix:** el `provider` del deployment es la única fuente de verdad;
el `model_id` se manda completo al upstream. Verificado: ahora enruta a `api.kilo.ai` con el model_id
íntegro. `services/router.py`. (La función `parse_model_id_prefix` se conserva con sus tests; solo se
retiró su uso dañino en el hot path.)

### 8. `/api/chains` deprecado visible en OpenAPI
Devolvía 410 Gone pero aparecía en `/docs`. **Fix:** `include_in_schema=False`. `main.py`.

### 9. `index` de tool_calls de Gemini en el proxy Anthropic
`openai_proxy` parcheaba el `index` ausente de Gemini, pero el translator del proxy Anthropic hacía
`tc.get("index", 0)` → varias tool calls colapsaban en el mismo bloque (index 0). El fix propuesto en
el informe (mover a `GeminiHandler.post_process_stream`) ya **no aplica**: en la v4 los handlers son
data-driven (`DataDrivenHandler` único), no hay clase por provider. **Fix:** fallback por posición
(`enumerate`) cuando falta `index`, consistente con `openai_proxy`. `services/translators.py`.

### 12. Endpoints sin tags OpenAPI
**Fix:** `tags=[...]` en los routers de dominio (instances, providers, deployments, router-settings,
router-stats, logs, provider-models) → `/docs` agrupado.

### 13. Config hardcoded (puertos, DB_PATH)
**Fix:** overridables por env con defaults estables: `INFINITY_DB_PATH` (`db.py`), `INFINITY_HOST`,
`INFINITY_PORT_OPENAI`, `INFINITY_PORT_ANTHROPIC` (`main.py`). Sin cambios de comportamiento por defecto.

---

## Reclasificados tras verificar el código

### 7. `router_settings` sin validación de tipos — ⚠️ Mitigado
El impacto reclamado ("falla en runtime al hacer `float()`") **no ocurre**: `Router._reload_cache`
parsea `allowed_fails`, `cooldown_times` y `cooldown_time` con `try/except` defensivo y descarta
valores inválidos sin crashear. Añadir Pydantic por-setting sigue siendo una mejora de robustez válida
(rechazar antes en el PUT), pero no es un bug: el runtime ya es tolerante. Se deja documentado, no se
fuerza para no arriesgar rechazar formas de valor válidas (p.ej. `cooldown_times` es un dict).

### 10. `resolve_claude_model` hardcoded — ❌ Falso positivo
`resolve_claude_model` solo mapea `claude-* → tier` (`opus`/`sonnet`/`haiku`). Ese tier pasa después
por `model_group_alias` en `acompletion`, que **sí** es configurable en `router_settings`. La
flexibilidad que pedía el hallazgo (p.ej. `sonnet → infinity/sonnet-long`) ya existe vía alias, sin
tocar código.

### 11. Benchmark test faltante — ❌ Falso positivo
El hallazgo citaba `specs/minimal-latency-overhead/spec.md`. Ese directorio `specs/` **ya no existe**
en el repo, así que la "requirement" que lo exigía desapareció. No hay gate a incumplir.

---

## Falsos positivos originales (del análisis inicial — NO fixear)

| # | Item | Razón |
|---|------|-------|
| FP-1 | `mark_success` resetea todos los error types | Diseño correcto (200 = deployment sano), igual que LiteLLM |
| FP-2 | Bucketing 1-min hardcoded | Es la ventana de conteo, no el cooldown (que sí es configurable) |
| FP-3 | `_filter_rate_limited` clean-up | Conserva `b-1, b`, exactamente lo que suma el filtro |
| FP-4 | `model_group_alias` no recursivo | Intencional; evita loops (LiteLLM tampoco lo hace) |
| FP-5 | `_deployment_to_dict` crea dicts | El fast-path (1 deployment) lo skipea; overhead <1µs |
| FP-6 | `router_stats` sort por request | Endpoint de UI manual, no hot path |
| FP-7 | ProviderHandler sin `supports_streaming` | Feature futura; todos los providers actuales streamean |

---

## Verificación

```bash
.venv/bin/python3 -m pytest tests/ -q       # 74 pasan
ruff check services/ routers/ db.py main.py  # limpio
```

*Documento actualizado 2026-07-22 tras resolver y re-verificar contra el código real.*
