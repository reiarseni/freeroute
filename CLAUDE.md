# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Idioma

Escribe y comenta en español de España.

## Qué es

**FreeRoute** (antes *Infinity Provisioner*) es un **proxy local de rotación multi-provider**
para herramientas de coding AI (Claude Code, Cline, KiloCode, OpenCode, Aider) y cualquier
cliente OpenAI-compatible. Intercepta las peticiones, las enruta a modelos gratuitos con
fallback automático entre varios proveedores y varias API keys, y sobrevive a rate limits
sin interrumpir la sesión.

> Nota sobre naming: el nombre público y las env vars/headers/DB (`FREEROUTE_*`, `X-FreeRoute-*`,
> `~/.freeroute.db`, `~/.freeroute-cooldowns.json`) usan **FreeRoute**. Al arrancar, si existe el
> fichero legacy `~/.infinity-provisioner-cooldowns.json` se renombra automáticamente al nuevo
> nombre (sin pérdida de datos).

## Arranque y verificación

```bash
# Instalar deps (primera vez)
.venv/bin/pip install -r requirements.txt

# Levantar (foreground, desarrollo) — libera los puertos 8787/8788 y arranca ambos proxies
.venv/bin/python3 main.py

# Background persistente (NO usar `python3 main.py &` — muere con el shell)
setsid .venv/bin/python3 main.py > /tmp/freeroute.log 2>&1 &

# Salud
curl -s http://localhost:8787/api/health | python3 -m json.tool
```

- **8787** → proxy OpenAI-compatible (`/v1/chat/completions`) + REST API (`/api/*`) + SPA Svelte.
- **8788** → proxy Anthropic-compatible (`/v1/messages`) para Claude Code.

## Tests

```bash
# Suite unitaria (pytest + pytest_asyncio, mockea los upstream — no necesita red ni servidor)
.venv/bin/python3 -m pytest tests/ -q

# Un solo archivo o test
.venv/bin/python3 -m pytest tests/test_router_core.py -q
.venv/bin/python3 -m pytest tests/test_router_core.py::test_fallback_chain -q

# Lint (única herramienta instalada)
ruff check services/ routers/ db.py main.py

# smoke_test.py NO es unitario: golpea un servidor en marcha en :8787/:8788
```

## Arquitectura

El sistema es **data-driven desde SQLite** (`~/.freeroute.db`). Nada de providers,
modelos ni cadenas de fallback está hardcodeado: todo vive en tablas y se edita por la REST API / SPA.

```
main.py                      Entry point: lanza dos apps FastAPI en threads (8787 y 8788)
  routers/                   Un archivo por dominio HTTP
    openai_proxy.py          POST /v1/chat/completions → Router (passthrough OpenAI)
    anthropic_proxy.py       POST /v1/messages → traduce Anthropic↔OpenAI → Router
    api_keys.py              CRUD /api/instances (API keys por provider)
    providers.py             CRUD /api/providers (base_url, auth, kind, headers)
    deployments.py           CRUD /api/deployments (model_name → provider+model_id ordenados)
    router_settings.py       /api/router-settings (strategy, fallbacks, cooldowns, alias)
    router_stats.py          /api/router-stats (latencia p50/p95, cooldowns, in-flight)
    logs.py                  /api/logs · chains.py → 410 Gone (deprecado)
  services/
    router.py                Orquestador LiteLLM-style: acompletion(model_name, body, stream)
    cooldown.py              Cooldown por deployment_id, bucketing por minuto, persistido a JSON
    routing_strategies.py    positional · simple-shuffle · latency-based · least-busy
    provider_handler.py      DataDrivenHandler: arma URL/headers/body por provider desde DB
    translators.py           Anthropic↔OpenAI, incl. streaming SSE incremental
  db.py                      SQLite async (aiosqlite): schema, migraciones, helpers CRUD
  static/                    SPA Svelte compilada · frontend/ es el source (Vite)
```

### Flujo de una petición

1. El proxy recibe `model` y llama `router.acompletion(model_name, body, stream)`.
2. `acompletion` resuelve `model_name` vía `model_group_alias` (router_settings), busca los
   **deployments habilitados** con ese `model_name`, descarta los que están en cooldown, aplica
   throttle RPM/TPM y elige uno con la routing strategy activa.
3. `_try_deployment` construye la request con el `DataDrivenHandler` del provider del deployment,
   la dispara con `httpx` (HTTP/2, streaming) y clasifica el resultado.
4. Si falla, marca cooldown por tipo de error y prueba el siguiente deployment; agotados todos,
   recorre las cadenas de `fallbacks` / `default_fallbacks`. Sin nada → 503.

### Resolución de modelos (Claude Code)

`resolve_claude_model` (translators.py) mapea el modelo que pide Claude Code a un **tier**:
`claude-opus-* → opus`, `claude-sonnet-* → sonnet`, resto → `haiku`. Ese tier es un `model_name`
que debe existir como `model_name` en la tabla `deployments` (p.ej. `infinity/haiku`), opcionalmente
re-mapeado por `model_group_alias`. La cadena de fallback de cada tier se define por sus deployments
ordenados (`order`) más las entradas de `fallbacks` en router_settings — todo editable en la UI, no
en código.

## Convenciones

- **FastAPI** (no Flask), un router por dominio en `routers/`, lógica en `services/`.
- **SQLite async** vía `aiosqlite`; nunca `sqlite3` síncrono.
- **Streaming siempre**: ningún endpoint bufferiza la respuesta completa. Al decodificar bytes de
  un stream usa un decoder UTF-8 incremental (`codecs.getincrementaldecoder`), no `bytes.decode`
  por chunk (parte caracteres multibyte).
- **Timeouts explícitos** en todo request de red.
- Las URLs y headers de cada provider salen del `DataDrivenHandler` (tabla `providers`) —
  no hardcodear bases ni construir headers en otros módulos.
- Al mutar providers/deployments/settings, invalidar la caché del router (`app_router.invalidate_cache()`).

## Archivos que el código toca

Al arrancar, `db.init_db()` siembra los providers por defecto (`SEED_PROVIDERS` en `db.py`) y los
router settings por defecto. Las API keys (api_instances) NO se auto-cargan: se crean por la UI /
`POST /api/instances`.

```
.env                            # Keys sueltas del desarrollador (NO commitear); el código NO las lee
~/.freeroute.db                  # SQLite async (providers, deployments, api_instances, settings, logs)
~/.freeroute-cooldowns.json  # Estado de cooldown persistido (atomic rename)
/tmp/freeroute.log               # Log del servidor en modo background
```

## Git Safety

Antes de cualquier `git restore/clean/checkout` destructivo: **`git stash` siempre**, aunque creas
que no hay cambios. Si hace falta crear una rama sin stashear primero, pregunta antes.

## Commits

**Nunca** añadas trailers `Co-Authored-By: Claude...` ni `Claude-Session: ...` a los mensajes de
commit de este repo, aunque la plantilla por defecto del sistema los incluya. El usuario los ha
pedido eliminar explícitamente más de una vez.
