"""
Base de datos SQLite — schema y helpers async.
Archivo: ~/.freeroute.db
"""

import json
import os
from pathlib import Path

import aiosqlite

# Ruta de la DB configurable por env (FREEROUTE_DB_PATH); default estable.
DB_PATH = Path(os.getenv("FREEROUTE_DB_PATH", str(Path.home() / ".freeroute.db")))

SCHEMA = """
-- Providers editables (data-driven). Reemplaza el hardcode de
-- services/provider_handler.py. El `name` es el identificador que referencian
-- api_instances.provider y deployments.provider (renombrable en cascada).
CREATE TABLE IF NOT EXISTS providers (
    name          TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    base_url      TEXT NOT NULL,
    models_url    TEXT NOT NULL,
    auth_type     TEXT NOT NULL DEFAULT 'bearer' CHECK(auth_type IN ('bearer','keyless','static','oauth_device')),
    auth_value    TEXT NOT NULL DEFAULT '',
    extra_headers TEXT NOT NULL DEFAULT '{}',
    kind          TEXT NOT NULL DEFAULT 'plain',
    created_at    TEXT DEFAULT (datetime('now'))
);

-- api_instances.provider ya NO tiene CHECK: referencia (lógicamente) providers.name.
-- oauth_state: JSON con credenciales OAuth (client_id/secret, tokens, expiración,
-- status) para instancias de providers auth_type=oauth_device. Vacío ('{}') para
-- el resto.
CREATE TABLE IF NOT EXISTS api_instances (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    provider   TEXT NOT NULL,
    api_key    TEXT NOT NULL,
    is_free    INTEGER NOT NULL DEFAULT 1,
    oauth_state TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Deprecada en v5: mantenida por compatibilidad, no se usa.
-- chain_slots se reemplaza por la tabla deployments (ver abajo).
CREATE TABLE IF NOT EXISTS chain_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id        TEXT NOT NULL CHECK(chain_id IN ('haiku', 'sonnet', 'opus')),
    position        INTEGER NOT NULL,
    api_instance_id TEXT NOT NULL REFERENCES api_instances(id) ON DELETE CASCADE,
    model_id        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name       TEXT NOT NULL,
    provider         TEXT,
    api_instance_id  TEXT REFERENCES api_instances(id) ON DELETE CASCADE,
    model_id         TEXT NOT NULL,
    weight           REAL DEFAULT 1.0,
    rpm              INTEGER,
    tpm              INTEGER,
    max_input_tokens INTEGER,
    "order"          INTEGER DEFAULT 0,
    enabled          INTEGER DEFAULT 1,
    created_at       TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deployments_model_order
    ON deployments(model_name, "order");

CREATE TABLE IF NOT EXISTS router_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    provider        TEXT NOT NULL DEFAULT '',
    api_instance_id TEXT NOT NULL REFERENCES api_instances(id) ON DELETE CASCADE,
    model_id        TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0,
    rpm             INTEGER NOT NULL DEFAULT 0,
    tpm             INTEGER NOT NULL DEFAULT 0,
    max_input_tokens INTEGER NOT NULL DEFAULT 0,
    max_parallel_requests INTEGER NOT NULL DEFAULT 0,
    "order"         INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(model_name, "order")
);

CREATE TABLE IF NOT EXISTS router_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT DEFAULT (datetime('now')),
    proxy_type      TEXT,
    chain_id        TEXT,
    original_model  TEXT,
    api_instance_id TEXT,
    model_id        TEXT,
    status_code     INTEGER,
    latency_ms      INTEGER,
    model_name      TEXT,
    error_type      TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Migración: ampliar el CHECK constraint de provider si la tabla ya existía
        async with db.execute("PRAGMA table_info(api_instances)") as cur:
            pass
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='api_instances'"
        ) as cur:
            row = await cur.fetchone()
            schema_sql = row[0] if row else ""
        # Migración: eliminar el CHECK constraint hardcodeado de provider.
        # Ahora los providers son data-driven (tabla providers) y renombrables,
        # así que api_instances.provider debe ser TEXT libre.
        if "CHECK(provider" in schema_sql:
            await db.executescript("""
                CREATE TABLE api_instances_new (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    provider   TEXT NOT NULL,
                    api_key    TEXT NOT NULL,
                    is_free    INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO api_instances_new SELECT * FROM api_instances;
                DROP TABLE api_instances;
                ALTER TABLE api_instances_new RENAME TO api_instances;
            """)
        # Migración: añadir max_parallel_requests a deployments si la tabla es previa.
        async with db.execute("PRAGMA table_info(deployments)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "max_parallel_requests" not in cols:
            await db.execute(
                "ALTER TABLE deployments ADD COLUMN max_parallel_requests INTEGER NOT NULL DEFAULT 0"
            )

        # Migración: ampliar el CHECK de providers.auth_type para incluir oauth_device
        # (SQLite no soporta ALTER de un CHECK — recrear tabla, mismo patrón que arriba).
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='providers'"
        ) as cur:
            row = await cur.fetchone()
            providers_sql = row[0] if row else ""
        if providers_sql and "oauth_device" not in providers_sql:
            await db.executescript("""
                CREATE TABLE providers_new (
                    name          TEXT PRIMARY KEY,
                    label         TEXT NOT NULL DEFAULT '',
                    base_url      TEXT NOT NULL,
                    models_url    TEXT NOT NULL,
                    auth_type     TEXT NOT NULL DEFAULT 'bearer'
                        CHECK(auth_type IN ('bearer','keyless','static','oauth_device')),
                    auth_value    TEXT NOT NULL DEFAULT '',
                    extra_headers TEXT NOT NULL DEFAULT '{}',
                    kind          TEXT NOT NULL DEFAULT 'plain',
                    created_at    TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO providers_new SELECT * FROM providers;
                DROP TABLE providers;
                ALTER TABLE providers_new RENAME TO providers;
            """)

        # Migración: añadir oauth_state a api_instances si la tabla es previa.
        async with db.execute("PRAGMA table_info(api_instances)") as cur:
            inst_cols = {row[1] for row in await cur.fetchall()}
        if "oauth_state" not in inst_cols:
            await db.execute(
                "ALTER TABLE api_instances ADD COLUMN oauth_state TEXT NOT NULL DEFAULT '{}'"
            )

        # Migración: añadir model_name/error_type a request_logs si la tabla es
        # previa (estaban en SCHEMA pero CREATE TABLE IF NOT EXISTS no las añade
        # a una tabla ya creada sin ellas).
        async with db.execute("PRAGMA table_info(request_logs)") as cur:
            log_cols = {row[1] for row in await cur.fetchall()}
        if "model_name" not in log_cols:
            await db.execute("ALTER TABLE request_logs ADD COLUMN model_name TEXT")
        if "error_type" not in log_cols:
            await db.execute("ALTER TABLE request_logs ADD COLUMN error_type TEXT")

        # Migración: el endpoint de Z.AI cambió de /api/v1 a /api/paas/v4
        # (el antiguo dejó de resolver). Solo se corrige si sigue apuntando
        # al valor histórico, para no pisar una URL personalizada del usuario.
        await db.execute(
            """UPDATE providers SET base_url = 'https://api.z.ai/api/paas/v4',
               models_url = 'https://api.z.ai/api/paas/v4/models', kind = 'zai'
               WHERE name = 'zai' AND base_url = 'https://api.z.ai/api/v1'"""
        )

        await db.commit()

    # Seed default router_settings
    await seed_router_defaults()
    # Seed providers (los 10 hardcodeados históricos) si la tabla está vacía
    await seed_providers()
    # Migrar chain_slots legacy → deployments (una vez, sólo si deployments está vacío)
    await migrate_chain_slots_to_deployments()


async def migrate_chain_slots_to_deployments():
    """Si la tabla chain_slots tiene datos legacy y no existen deployments
    con model_name IN ('infinity/haiku','infinity/sonnet','infinity/opus'),
    migra los chain_slots al nuevo esquema.
    Idempotente: no duplica si ya existen.
    """
    legacy_tiers = {"infinity/haiku", "infinity/sonnet", "infinity/opus"}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Check cuáles tiers ya existen en deployments
        placeholders = ",".join("?" for _ in legacy_tiers)
        async with db.execute(
            f"SELECT model_name FROM deployments WHERE model_name IN ({placeholders})",
            list(legacy_tiers),
        ) as cur:
            existing = {row["model_name"] for row in await cur.fetchall()}
        # Leer chain_slots
        async with db.execute(
            "SELECT chain_id, position, api_instance_id, model_id FROM chain_slots ORDER BY chain_id, position"
        ) as cur:
            legacy = await cur.fetchall()
        if not legacy:
            return
        # Obtener max order por model_name existente para evitar UNIQUE conflict
        async with db.execute(
            'SELECT model_name, MAX("order") AS max_order FROM deployments GROUP BY model_name'
        ) as cur:
            max_orders = {row["model_name"]: row["max_order"] or 0 for row in await cur.fetchall()}
        for s in legacy:
            model_name = f"infinity/{s['chain_id']}"
            if model_name in existing:
                continue  # ya existe, saltar
            async with db.execute(
                "SELECT provider FROM api_instances WHERE id = ?", (s["api_instance_id"],)
            ) as cur2:
                inst = await cur2.fetchone()
            provider = inst["provider"] if inst else ""
            # Offset para evitar UNIQUE(model_name, order)
            base_order = max_orders.get(model_name, 0) + 1
            await db.execute(
                """INSERT INTO deployments
                   (model_name, provider, api_instance_id, model_id, weight, "order", enabled)
                   VALUES (?, ?, ?, ?, 1.0, ?, 1)""",
                (model_name, provider, s["api_instance_id"], s["model_id"],
                 base_order + s["position"]),
            )
        await db.commit()


ROUTER_DEFAULTS = {
    "routing_strategy": "positional",
    "allowed_fails": {
        "RATE_LIMIT": 1,
        "MODEL_NOT_FOUND": 2,
        "SERVER_ERROR": 3,
        "AUTH_ERROR": 1,
        "TIMEOUT": 3,
        "CONTENT_POLICY_VIOLATION": 1,
        "CONTEXT_WINDOW_EXCEEDED": 1,
        "UNKNOWN": 2,
    },
    "cooldown_times": {
        "RATE_LIMIT": 60.0,
        "MODEL_NOT_FOUND": 300.0,
        "SERVER_ERROR": 60.0,
        "AUTH_ERROR": 300.0,
        "TIMEOUT": 30.0,
        "CONTENT_POLICY_VIOLATION": 0.0,
        "CONTEXT_WINDOW_EXCEEDED": 0.0,
        "UNKNOWN": 60.0,
    },
    "cooldown_time": 60.0,
    "fallbacks": {},
    "context_window_fallbacks": {},
    "content_policy_fallbacks": {},
    "default_fallbacks": [],
    "model_group_alias": {},
    "enable_pre_call_checks": False,
    "hanging_threshold": 30.0,
    # {model_name: bool} — si True, ese model_name ignora `routing_strategy`
    # global y siempre elige el deployment con menos RPM consumido en la
    # ventana actual (cooldown/rate-limit se evalúan igual, antes de esto).
    "least_rpm_models": {},
}


async def seed_router_defaults():
    """Inserta defaults en router_settings si no existen y limpia claves obsoletas."""
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in ROUTER_DEFAULTS.items():
            await db.execute(
                "INSERT OR IGNORE INTO router_settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        # num_retries quedó obsoleto cuando el router pasó a 1 intento por
        # deployment con fallback entre deployments como retry. Lo borramos
        # para que no confunda en la API de settings.
        await db.execute("DELETE FROM router_settings WHERE key = 'num_retries'")
        await db.commit()


# ── Providers ────────────────────────────────────────────────────────────────

# Seed de los providers históricamente hardcodeados en provider_handler.py.
# auth_type: bearer (usa api_key de la instancia) | keyless | static (auth_value literal).
# extra_headers: JSON de headers extra. kind: sabor de parseo de /models (estable al renombrar).
SEED_PROVIDERS = [
    {"name": "openrouter", "label": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1",
     "models_url": "https://openrouter.ai/api/v1/models",
     "auth_type": "bearer", "auth_value": "",
     "extra_headers": {"HTTP-Referer": "http://localhost:8787", "X-Title": "FreeRoute"},
     "kind": "openrouter"},
    {"name": "deepseek", "label": "DeepSeek",
     # OpenAI-compatible. Requiere API key de platform.deepseek.com.
     "base_url": "https://api.deepseek.com/v1",
     "models_url": "https://api.deepseek.com/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "cerebras", "label": "Cerebras",
     # OpenAI-compatible, inferencia ultra-baja latencia. Requiere API key.
     "base_url": "https://api.cerebras.ai/v1",
     "models_url": "https://api.cerebras.ai/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "sambanova", "label": "SambaNova",
     # OpenAI-compatible. Tier gratuito generoso para Meta-Llama-3.1-405B etc.
     "base_url": "https://api.sambanova.ai/v1",
     "models_url": "https://api.sambanova.ai/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "gemini", "label": "Gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "gemini"},
    {"name": "groq", "label": "Groq",
     "base_url": "https://api.groq.com/openai/v1",
     "models_url": "https://api.groq.com/openai/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "zai", "label": "Z.AI",
     "base_url": "https://api.z.ai/api/paas/v4",
     "models_url": "https://api.z.ai/api/paas/v4/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "zai"},
    {"name": "zen", "label": "Zen",
     "base_url": "https://opencode.ai/zen/v1",
     "models_url": "https://opencode.ai/zen/v1/models",
     "auth_type": "keyless", "auth_value": "", "extra_headers": {}, "kind": "zen"},
    {"name": "zen-auth", "label": "Zen (API key)",
     "base_url": "https://opencode.ai/zen/v1",
     "models_url": "https://opencode.ai/zen/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "kilo", "label": "Kilo",
     "base_url": "https://api.kilo.ai/api/openrouter",
     "models_url": "https://api.kilo.ai/api/openrouter/models",
     "auth_type": "static", "auth_value": "Bearer anonymous", "extra_headers": {}, "kind": "kilo"},
    {"name": "nvidia", "label": "NVIDIA",
     "base_url": "https://integrate.api.nvidia.com/v1",
     "models_url": "https://integrate.api.nvidia.com/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "nvidia"},
    {"name": "ollama", "label": "Ollama",
     "base_url": "https://ollama.com/v1",
     "models_url": "https://ollama.com/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "ollama-local", "label": "Ollama Local",
     "base_url": "http://localhost:11434/v1",
     "models_url": "http://localhost:11434/v1/models",
     "auth_type": "keyless", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "mimo", "label": "Xiaomi MiMo",
     "base_url": "https://api.xiaomimimo.com/v1",
     "models_url": "https://api.xiaomimimo.com/v1/models",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "plain"},
    {"name": "cloudflare", "label": "Cloudflare Workers AI",
     # OpenAI-compatible endpoint. El account_id va embebido en la URL; el resto
     # (/chat/completions) lo añade el DataDrivenHandler.
     # ⚠️ Reemplaza <YOUR_CF_ACCOUNT_ID> por tu account_id (Cloudflare dashboard,
     # right sidebar) y añade una API key (Bearer) con permisos Workers AI.
     "base_url": "https://api.cloudflare.com/client/v4/accounts/<YOUR_CF_ACCOUNT_ID>/ai/v1",
     # /v1/models no soporta GET en Cloudflare: usamos el endpoint de búsqueda,
     # que además ya excluye modelos deprecados. Formato {"result":[{"name":...}]}.
     "models_url": "https://api.cloudflare.com/client/v4/accounts/<YOUR_CF_ACCOUNT_ID>/ai/models/search?task=Text+Generation&per_page=100",
     "auth_type": "bearer", "auth_value": "", "extra_headers": {}, "kind": "cloudflare"},
    {"name": "kiro", "label": "Kiro (AWS SSO OIDC)",
     # AWS SSO OIDC (Device Authorization Grant, RFC 8628). base_url apunta a la
     # región por defecto; region overrideable por instancia vía oauth_state.region.
     "base_url": "https://oidc.us-east-1.amazonaws.com",
     "models_url": "",
     "auth_type": "oauth_device", "auth_value": "", "extra_headers": {}, "kind": "kiro"},
]


async def seed_providers():
    """Inserta los providers seed si no existen (idempotente, INSERT OR IGNORE)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for p in SEED_PROVIDERS:
            await db.execute(
                """INSERT OR IGNORE INTO providers
                   (name, label, base_url, models_url, auth_type, auth_value, extra_headers, kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (p["name"], p["label"], p["base_url"], p["models_url"],
                 p["auth_type"], p["auth_value"], json.dumps(p["extra_headers"]), p["kind"]),
            )
        await db.commit()


def _parse_provider_row(row: dict) -> dict:
    """Deserializa extra_headers (TEXT JSON → dict)."""
    out = dict(row)
    try:
        out["extra_headers"] = json.loads(out.get("extra_headers") or "{}")
    except (ValueError, TypeError):
        out["extra_headers"] = {}
    return out


async def get_providers() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM providers ORDER BY name") as cur:
            return [_parse_provider_row(dict(r)) for r in await cur.fetchall()]


async def get_provider(name: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM providers WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
            return _parse_provider_row(dict(row)) if row else None


async def create_provider(data: dict) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO providers
               (name, label, base_url, models_url, auth_type, auth_value, extra_headers, kind)
               VALUES (:name, :label, :base_url, :models_url, :auth_type, :auth_value, :extra_headers, :kind)""",
            {**data, "extra_headers": json.dumps(data.get("extra_headers") or {})},
        )
        await db.commit()
    return await get_provider(data["name"])


async def update_provider(name: str, data: dict) -> dict | None:
    """Actualiza un provider. Si cambia el `name`, propaga en cascada a
    api_instances.provider y deployments.provider dentro de una transacción."""
    new_name = data.get("name", name)
    fields = {
        "name": new_name,
        "label": data.get("label", ""),
        "base_url": data["base_url"],
        "models_url": data["models_url"],
        "auth_type": data.get("auth_type", "bearer"),
        "auth_value": data.get("auth_value", ""),
        "extra_headers": json.dumps(data.get("extra_headers") or {}),
        "kind": data.get("kind", "plain"),
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN")
        await db.execute(
            """UPDATE providers SET
                 name=:name, label=:label, base_url=:base_url, models_url=:models_url,
                 auth_type=:auth_type, auth_value=:auth_value,
                 extra_headers=:extra_headers, kind=:kind
               WHERE name=:old_name""",
            {**fields, "old_name": name},
        )
        if new_name != name:
            # Cascada: renombrar todas las referencias
            await db.execute("UPDATE api_instances SET provider=? WHERE provider=?", (new_name, name))
            await db.execute("UPDATE deployments SET provider=? WHERE provider=?", (new_name, name))
        await db.commit()
    return await get_provider(new_name)


async def delete_provider(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM providers WHERE name = ?", (name,))
        deleted = cur.rowcount > 0
        await db.commit()
        return deleted


# ── API Instances ────────────────────────────────────────────────────────────

async def get_all_instances() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM api_instances ORDER BY created_at") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_instance(instance_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM api_instances WHERE id = ?", (instance_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_instance(data: dict):
    """Crea/actualiza una instancia. `oauth_state` es opcional: si no se pasa,
    se preserva el valor existente (para no perder el estado OAuth cuando la
    instancia se edita por el flujo genérico de API keys)."""
    async with aiosqlite.connect(DB_PATH) as db:
        oauth_state = data.get("oauth_state")
        if oauth_state is None:
            async with db.execute(
                "SELECT oauth_state FROM api_instances WHERE id = ?", (data["id"],)
            ) as cur:
                row = await cur.fetchone()
            oauth_state = _parse_oauth_state(row[0]) if row else {}
        await db.execute(
            """INSERT INTO api_instances (id, name, provider, api_key, is_free, oauth_state)
               VALUES (:id, :name, :provider, :api_key, :is_free, :oauth_state)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, provider=excluded.provider,
                 api_key=excluded.api_key, is_free=excluded.is_free,
                 oauth_state=excluded.oauth_state""",
            {**data, "oauth_state": json.dumps(oauth_state)},
        )
        await db.commit()


async def delete_instance(instance_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM api_instances WHERE id = ?", (instance_id,))
        await db.commit()


def _parse_oauth_state(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}


def get_instance_oauth_state(instance: dict) -> dict:
    """Deserializa oauth_state (TEXT JSON) de una fila de api_instances."""
    return _parse_oauth_state(instance.get("oauth_state"))


async def set_oauth_state(instance_id: str, state: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE api_instances SET oauth_state = ? WHERE id = ?",
            (json.dumps(state), instance_id),
        )
        await db.commit()


# ── Chain Slots ──────────────────────────────────────────────────────────────

async def get_chain_slots(chain_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT cs.*, ai.name as instance_name, ai.provider, ai.is_free
               FROM chain_slots cs
               JOIN api_instances ai ON ai.id = cs.api_instance_id
               WHERE cs.chain_id = ?
               ORDER BY cs.position""",
            (chain_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def replace_chain_slots(chain_id: str, slots: list[dict]):
    """Reemplaza todos los slots de una cadena con el array ordenado recibido."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chain_slots WHERE chain_id = ?", (chain_id,))
        for i, slot in enumerate(slots):
            await db.execute(
                "INSERT INTO chain_slots (chain_id, position, api_instance_id, model_id) VALUES (?, ?, ?, ?)",
                (chain_id, i, slot["api_instance_id"], slot["model_id"]),
            )
        await db.commit()


# ── Request Logs ─────────────────────────────────────────────────────────────

async def insert_log(data: dict):
    data = {"error_type": None, **data}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO request_logs
               (proxy_type, chain_id, original_model, api_instance_id,
                model_id, status_code, latency_ms, error_type)
               VALUES (:proxy_type, :chain_id, :original_model, :api_instance_id,
                       :model_id, :status_code, :latency_ms, :error_type)""",
            data,
        )
        await db.commit()


async def cleanup_logs(max_rows: int = 50_000):
    """Elimina logs más antiguos si se supera max_rows."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM request_logs WHERE id NOT IN "
            "(SELECT id FROM request_logs ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        await db.commit()


async def get_health_stats() -> dict:
    """Retorna estadísticas para el endpoint /api/health."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Slots por cadena
        async with db.execute(
            "SELECT chain_id, COUNT(*) as n FROM chain_slots GROUP BY chain_id"
        ) as cur:
            rows = await cur.fetchall()
            chains = {r["chain_id"]: r["n"] for r in rows}

        # Stats últimas 24h
        async with db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status_code != 200 THEN 1 ELSE 0 END) as errors,
                CAST(AVG(latency_ms) AS INTEGER) as avg_latency_ms
               FROM request_logs
               WHERE ts > datetime('now', '-24 hours')"""
        ) as cur:
            row = await cur.fetchone()
            logs_24h = dict(row) if row else {"total": 0, "errors": 0, "avg_latency_ms": 0}

        return {"chains": chains, "logs_24h": logs_24h}


async def get_logs(limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Deployments ─────────────────────────────────────────────────────────────

async def get_deployments(model_name: str | None = None) -> list[dict]:
    """Lista deployments, opcionalmente filtrados por model_name.

    Ordena por model_name asc y order asc para que el PositionalStrategy
    y el frontend obtengan el orden canónico.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if model_name is None:
            sql = 'SELECT * FROM deployments ORDER BY model_name ASC, "order" ASC'
            params: tuple = ()
        else:
            sql = 'SELECT * FROM deployments WHERE model_name = ? ORDER BY "order" ASC'
            params = (model_name,)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_distinct_model_names() -> list[dict]:
    """Lista todos los model_name con su count de deployments."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT model_name, COUNT(*) AS count FROM deployments GROUP BY model_name ORDER BY model_name'
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def create_deployment(data: dict) -> dict:
    """Crea un deployment. Devuelve la fila creada (incluye id y created_at)."""
    data.setdefault("max_parallel_requests", 0)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """INSERT INTO deployments
               (model_name, provider, api_instance_id, model_id, weight,
                rpm, tpm, max_input_tokens, max_parallel_requests, "order", enabled)
               VALUES (:model_name, :provider, :api_instance_id, :model_id, :weight,
                :rpm, :tpm, :max_input_tokens, :max_parallel_requests, :order, :enabled)""",
            data,
        ) as cur:
            new_id = cur.lastrowid
        #_si el caller no envió 'provider', deriva de la instancia (mejor esfuerzo)
        await db.commit()
    # Devolver la fila completa
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deployments WHERE id = ?", (new_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def update_deployment(dep_id: int, data: dict) -> dict | None:
    """Actualiza campos de un deployment. `data` sólo con campos a actualizar."""
    allowed = {
        "model_name", "provider", "api_instance_id", "model_id", "weight",
        "rpm", "tpm", "max_input_tokens", "max_parallel_requests", "order", "enabled",
    }
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return None

    # Si el (model_name, order) resultante choca con otro deployment del mismo
    # tier, re-mapear order al final (max+1) para no violar UNIQUE(model_name, order).
    target_model = fields.get("model_name")
    target_order = fields.get("order")
    if target_model is not None and target_order is not None:
        async with aiosqlite.connect(DB_PATH) as ck:
            ck.row_factory = aiosqlite.Row
            async with ck.execute(
                'SELECT id FROM deployments WHERE model_name = ? AND "order" = ? AND id != ?',
                (target_model, target_order, dep_id),
            ) as cur:
                clash = await cur.fetchone()
        if clash:
            async with aiosqlite.connect(DB_PATH) as ck:
                async with ck.execute(
                    'SELECT COALESCE(MAX("order"), -1) FROM deployments WHERE model_name = ?',
                    (target_model,),
                ) as cur:
                    row = await cur.fetchone()
                    max_order = row[0] if row else -1
            fields["order"] = int(max_order) + 1

    set_clause = ", ".join(f'"{k}" = ?' for k in fields)
    params = list(fields.values()) + [dep_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f'UPDATE deployments SET {set_clause} WHERE id = ?', params
        )
        await db.commit()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_deployment(dep_id: int) -> bool:
    """Borra un deployment por id. Devuelve True si se borró algo."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM deployments WHERE id = ?", (dep_id,))
        deleted = cur.rowcount > 0
        await db.commit()
        return deleted


# ── Router Settings ──────────────────────────────────────────────────────────

async def get_router_settings() -> dict[str, "object"]:
    """Devuelve todos los router_settings como dict {key: parsed_json_value}.

    Las filas ausentes se rellenan con ROUTER_DEFAULTS.
    """
    result = dict(ROUTER_DEFAULTS)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM router_settings") as cur:
            rows = await cur.fetchall()
    for row in rows:
        try:
            result[row[0]] = json.loads(row[1])
        except (ValueError, TypeError):
            # Si el JSON está corrupto, dejamos el default
            continue
    return result


async def upsert_router_setting(key: str, value) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO router_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await db.commit()


async def bulk_upsert_router_settings(data: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in data.items():
            await db.execute(
                "INSERT INTO router_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        await db.commit()


async def delete_router_setting(key: str) -> bool:
    """Borra un setting para que revierta al default. Devuelve True si existía."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM router_settings WHERE key = ?", (key,))
        deleted = cur.rowcount > 0
        await db.commit()
    # Re-seed default si existe en ROUTER_DEFAULTS
    if key in ROUTER_DEFAULTS:
        await upsert_router_setting(key, ROUTER_DEFAULTS[key])
    return deleted
