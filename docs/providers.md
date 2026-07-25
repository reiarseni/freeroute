# Providers reference

Every provider FreeRoute ships with, where to get a key, the wire format, and what to
expect on the free tier. All of these are seeded into the `providers` table on first run
and editable from the UI — you can add or remove any OpenAI-compatible endpoint.

> **Quotas change often.** Treat the "Free tier" column as a rough guide and verify on the
> provider's site before relying on it. PRs to update this table are welcome.

## Cloud / API-key providers

| Provider | Base URL | Get a key | Wire format | Free tier (approx.) |
|---|---|---|---|---|
| **OpenRouter** | `openrouter.ai/api/v1` | [openrouter.ai/keys](https://openrouter.ai/keys) | OpenAI | Free models (`:free` suffix); paid otherwise. Rate-limited per model. |
| **DeepSeek** | `api.deepseek.com/v1` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) | OpenAI | Limited free credit on signup; then very cheap per token. |
| **Cerebras** | `api.cerebras.ai/v1` | [cloud.cerebras.ai](https://cloud.cerebras.ai) | OpenAI | Free tier with rate limits; ultra-low latency. |
| **SambaNova** | `api.sambanova.ai/v1` | [cloud.sambanova.ai/apis](https://cloud.sambanova.ai/apis) | OpenAI | Generous free tier; Llama 3.1 405B etc. |
| **Gemini** | `generativelanguage.googleapis.com/v1beta/openai` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | OpenAI-compat | Free tier (RPM/TPD limited) on most models. |
| **Groq** | `api.groq.com/openai/v1` | [console.groq.com/keys](https://console.groq.com/keys) | OpenAI | Free tier with RPM/TPD limits; very fast. |
| **Z.AI** | `api.z.ai/api/paas/v4` | [z.ai](https://z.ai) | OpenAI-compat (GLM) | Free dev tier; GLM-4.5 / GLM-4.6. |
| **NVIDIA** | `integrate.api.nvidia.com/v1` | [build.nvidia.com](https://build.nvidia.com) | OpenAI | 1000 free credits at signup; NIM models. |
| **Xiaomi MiMo** | `api.xiaomimimo.com/v1` | [xiaomimimo.com](https://www.xiaomimimo.com) | OpenAI | Free dev access to MiMo models. |
| **Cloudflare Workers AI** | `api.cloudflare.com/client/v4/accounts/<ID>/ai/v1` | [dash.cloudflare.com](https://dash.cloudflare.com) → Workers AI | OpenAI | Free daily allocation; **account ID goes in the URL**. |

## Keyless providers

Work with `Bearer anonymous` (or no auth). Use these to get a working setup before you've
fetched any API keys.

| Provider | Base URL | Notes |
|---|---|---|
| **Zen** | `opencode.ai/zen/v1` | OpenCode's free gateway. |
| **Zen (API key)** | `opencode.ai/zen/v1` | Same endpoint, auth'd — higher limits. |
| **Kilo** | `api.kilo.ai/api/openrouter` | OpenRouter-compatible; `Bearer anonymous`. |

## Local providers

| Provider | Base URL | Notes |
|---|---|---|
| **Ollama** | `ollama.com/v1` | Ollama's cloud; usually you want `ollama-local` instead. |
| **Ollama Local** | `localhost:11434/v1` | Your own machine. Run `ollama serve`. |

## OAuth providers

| Provider | Flow | Notes |
|---|---|---|
| **Kiro** | AWS SSO OIDC device authorization (RFC 8628) | AWS SSO sign-in via browser; tokens refreshed automatically. |

## Adding a custom provider

Any OpenAI-compatible endpoint works. From the UI's **Providers** tab:

1. Set **base_url** to the endpoint root (without `/chat/completions`).
2. Set **models_url** to its `GET /models` equivalent (used by the Models tab to list
   what's available).
3. Choose **auth_type**:
   - `bearer` — the API key from the API Instances tab goes in `Authorization: Bearer …`.
   - `keyless` — no auth header sent.
   - `static` — the literal `auth_value` is sent as-is (e.g. `Bearer anonymous`).
4. Set **kind** to match how `/models` responds (`plain` for OpenAI's `{data:[...]}`,
   `openrouter`, `gemini`, `zai`, `zen`, `kilo`, `nvidia`, `cloudflare`, `kiro` for the
   provider-specific shapes). Use `plain` if unsure.
5. Add any **extra_headers** as JSON (e.g. OpenRouter wants `HTTP-Referer` and `X-Title`).

Then create an **API Instance** (your key) and a **Deployment** binding a `model_name` to
this provider + a concrete `model_id`. The router picks it up immediately — no restart.

## Why some providers have a custom `kind`

OpenAI's `GET /models` returns `{data: [{id: "..."}]}`. Several providers deviate:

- **OpenRouter** adds pricing and context fields — `kind=openrouter` filters free models.
- **Gemini** returns `{models: [{name: "models/..."}]}` — needs name stripping.
- **Cloudflare** doesn't expose `GET /models`; we use the search endpoint and parse
  `{result: [{name: "..."}]}`.
- **Kiro** has no model list at all; models are static.

The `kind` only affects model *listing* in the UI. Inference always goes through the
standard OpenAI `/chat/completions` shape, so any provider whose chat endpoint is
OpenAI-compatible will route correctly regardless of `kind`.
