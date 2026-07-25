"""
REST API — Fetch modelos disponibles desde cada proveedor.
Filtra por free/paid según la instancia para no mostrar modelos inaccesibles.

Toda la info de base URL y headers se obtiene del handler registry.
"""

import httpx
from fastapi import APIRouter, HTTPException, Query

import db
from services.provider_handler import build_handler

router = APIRouter(prefix="/api/provider-models", tags=["provider-models"])


def _is_free_model(m: dict) -> bool:
    """Un modelo de OpenRouter es free si su precio de prompt es "0"."""
    pricing = m.get("pricing", {})
    return str(pricing.get("prompt", "1")) == "0"


@router.get("")
async def get_provider_models(
    instance_id: str = Query(...),
    filter: str = Query("auto"),
):
    instance = await db.get_instance(instance_id)
    if not instance:
        raise HTTPException(404, detail=f"Instancia '{instance_id}' no encontrada")

    provider = instance["provider"]
    api_key = instance["api_key"]
    is_free_instance = bool(instance["is_free"])

    cfg = await db.get_provider(provider)
    if not cfg:
        return {"provider": provider, "models": [], "is_free_instance": is_free_instance}

    if cfg.get("kind") == "kiro":
        # Kiro no expone un endpoint de listado real (ListAvailableModels no está
        # confirmado, fuera de alcance): lista estática de model IDs conocidos.
        from services.kiro_translator import KIRO_STATIC_MODELS
        return {"provider": provider, "models": KIRO_STATIC_MODELS, "is_free_instance": is_free_instance}

    if not cfg.get("models_url"):
        return {"provider": provider, "models": [], "is_free_instance": is_free_instance}

    handler = build_handler(cfg)
    url = handler.models_url
    # Headers derivados del auth del provider (data-driven). Para /models Gemini
    # espera GET sin body; el Content-Type no molesta.
    headers = await handler._headers({"api_key": api_key})
    # El sabor de parseo de la respuesta es estable al renombrar el provider.
    kind = cfg.get("kind", "plain")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            if kind == "openrouter":
                all_models = data.get("data", [])
                effective_filter = filter
                if effective_filter == "auto":
                    effective_filter = "free" if is_free_instance else "all"
                if effective_filter == "free":
                    filtered = [m for m in all_models if _is_free_model(m)]
                elif effective_filter == "paid":
                    filtered = [m for m in all_models if not _is_free_model(m)]
                else:
                    filtered = all_models
                if is_free_instance:
                    filtered = [m for m in filtered if _is_free_model(m)]
                models = sorted(m["id"] for m in filtered)
                return {"provider": provider, "models": models, "is_free_instance": is_free_instance}

            if kind == "gemini":
                models = []
                for m in data.get("data", []):
                    name = m.get("id", "")
                    name = name.removeprefix("models/")
                    if name:
                        models.append(name)
                return {"provider": provider, "models": sorted(models), "is_free_instance": is_free_instance}

            if kind == "zen":
                # zen model id list, solo los que terminan en -free
                models = sorted(
                    m["id"] for m in data.get("data", [])
                    if m.get("id", "").endswith("-free")
                )
                return {"provider": provider, "models": models, "is_free_instance": True}

            if kind == "kilo":
                # kilo usa el mismo body que OpenRouter (model list). Filtro :free
                models = sorted(
                    m["id"] for m in data.get("data", [])
                    if m.get("id", "").endswith(":free")
                )
                return {"provider": provider, "models": models, "is_free_instance": True}

            if kind == "nvidia":
                # nvidia: lista plana, filtrar solo modelos de chat (excluir embeddings, rerank, vision, etc.)
                all_ids = [m["id"] for m in data.get("data", []) if m.get("id")]
                # Excluir modelos que son claramente no-chat
                skip_prefixes = ("nvidia/nv-embed", "nvidia/nvclip", "nvidia/riva",
                                 "nvidia/gliner", "nvidia/nemoguard", "nvidia/nemoretriever",
                                 "nvidia/nemotron-parse", "nvidia/nemotron-3.5",
                                 "nvidia/nemotron-3-nano-omni", "nvidia/nemotron-nano-12b-v2-vl",
                                 "nvidia/llama-3.2-nemoretriever", "nvidia/llama-nemotron-embed",
                                 "nvidia/llama-nemotron-rerank", "nvidia/nemotron-3-embed",
                                 "nvidia/ising", "nvidia/cosmos", "nvidia/bevformer",
                                 "nvidia/sparsedrive", "nvidia/streampetr", "nvidia/vila",
                                 "nvidia/visual", "nvidia/retail", "nvidia/nv-dino",
                                 "nvidia/nv-grounding", "nvidia/ai-synthetic",
                                 "nvidia/neva", "nvidia/nv-embedqa", "nvidia/nv-rerankqa",
                                 "nvidia/rerank", "baai/", "snowflake/", "mistralai/mistral-7b-instruct",
                                 "mistralai/mixtral-8x7b", "mistralai/mixtral-8x22b",
                                 "google/codegemma", "google/gemma-2b", "google/recurrentgemma",
                                 "google/deplot", "google/paligemma",
                                 "ibm/", "01-ai/", "upstage/", "zyphra/", "bigcode/",
                                 "databricks/", "poolside/", "stepfun-ai/step-3.5",
                                 "nvidia/llama3-chatqa", "nvidia/mistral-nemo",
                                 "nvidia/nemotron-4", "nvidia/nemotron-mini",
                                 "microsoft/kosmos", "microsoft/phi-3",
                                 "writer/", "thinkingmachines/", "sarvamai/",
                                 "bytedance/", "abacusai/", "adept/",
                                 "qwen/qwen3-next", "qwen/qwen3.5-397b",
                                 "moonshotai/kimi-k2.6")
                chat_models = sorted(mid for mid in all_ids if not mid.startswith(skip_prefixes))
                return {"provider": provider, "models": chat_models, "is_free_instance": True}

            if kind == "zai":
                # El /models de Z.AI no expone pricing y ni siquiera lista los
                # modelos -flash gratuitos (confirmado con key real, jul/2026);
                # la tabla de precios oficial (docs.z.ai/guides/overview/pricing)
                # marca estos tres como Free. Se listan estáticos porque no hay
                # forma de derivarlos del API.
                return {
                    "provider": provider,
                    "models": ["glm-4.5-flash", "glm-4.6v-flash", "glm-4.7-flash"],
                    "is_free_instance": True,
                }

            if kind == "cloudflare":
                # Cloudflare devuelve {"result":[{"name":"@cf/..."}]} (no formato OpenAI).
                # El endpoint de búsqueda ya excluye modelos deprecados.
                models = sorted(
                    m["name"] for m in data.get("result", []) if m.get("name")
                )
                return {"provider": provider, "models": models, "is_free_instance": is_free_instance}

            # groq, zai, ollama, ollama-local — lista plana de ids
            models = sorted(m["id"] for m in data.get("data", []) if m.get("id"))
            return {"provider": provider, "models": models, "is_free_instance": True}

    except httpx.HTTPStatusError as e:
        raise HTTPException(502, detail=f"Error del proveedor {provider}: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, detail=f"Error al consultar {provider}: {e!s}")
