<script>
  let copied = $state({})

  function copy(key, text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
      } else {
        fallbackCopy(text)
      }
    } catch {
      fallbackCopy(text)
    }
    copied[key] = true
    setTimeout(() => { copied[key] = false }, 2000)
  }

  function fallbackCopy(text) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch {}
    document.body.removeChild(ta)
  }

  const claudeCodeBash = `mkdir -p .claude
cat > .claude/settings.local.json << 'EOF'
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788",
    "ANTHROPIC_API_KEY": "infinity-proxy"
  }
}
EOF
echo ".claude/settings.local.json" >> .gitignore`

  const claudeCodeJson = `{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788",
    "ANTHROPIC_API_KEY": "infinity-proxy"
  }
}`

  const claudeCodeVerify = `# Desde dentro del proyecto — debe aparecer en la pestaña Logs
claude -p "di pong"`

  const openCodeJson = `{
  "$schema": "https://opencode.ai/config.json",
  "agent": {
    "explore": {
      "mode": "subagent",
      "model": "infinity/haiku"
    }
  },
  "provider": {
    "infinity": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Infinity",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "infinity-proxy"
      },
      "models": {
        "infinity/sonnet": { "name": "Sonnet" },
        "infinity/haiku": { "name": "Haiku" },
        "infinity/opus": { "name": "Opus" }
      }
    }
  },
  "model": "infinity/sonnet"
}`

  const openCodeVerify = `opencode -m infinity/sonnet "di pong"
# Verificar que aparece en la pestaña Logs`

  const rooCodeVerify = `# Abrir un chat en RooCode y escribir "pong"
# Verificar que aparece en la pestaña Logs`

  const mimoCodeJson = `{
  "$schema": "https://mimo.xiaomi.com/mimocode/config.json",
  "model": "infinity/sonnet",
  "provider": {
    "infinity": {
      "name": "Infinity",
      "npm": "@ai-sdk/openai-compatible",
      "only_configured_models": true,
      "models": {
        "infinity/sonnet": { "name": "Sonnet" },
        "infinity/haiku": { "name": "Haiku" },
        "infinity/opus": { "name": "Opus" }
      },
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "infinity-proxy"
      }
    }
  }
}`

  const mimoCodeVerify = `mimo models
# Debe listar infinity/sonnet, infinity/haiku, infinity/opus
mimo -m infinity/sonnet "di pong"
# Verificar que aparece en la pestaña Logs`

  const hermesConfigYaml = `model:
  default: infinity/sonnet
  provider: custom
  base_url: http://127.0.0.1:8787/v1
  api_key: infinity-proxy`

  const hermesVerify = `hermes chat --provider custom --model infinity/sonnet
# di "pong" y verificá que aparece en la pestaña Logs

# Cambiar de modelo dentro de una sesión activa:
# /model custom:infinity/haiku
# /model custom:infinity/opus`
</script>

{#snippet codeblock(id, code)}
  <div class="relative group">
    <pre class="bg-gray-950 border border-gray-800 rounded-xl px-4 py-3 text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre leading-relaxed">{code}</pre>
    <button
      onclick={() => copy(id, code)}
      class="absolute top-2 right-2 px-2 py-1 rounded text-xs transition-all
        {copied[id]
          ? 'bg-green-800/60 text-green-300 border border-green-700'
          : 'bg-gray-800 text-gray-500 border border-gray-700 opacity-0 group-hover:opacity-100 hover:text-gray-200'}"
    >
      {copied[id] ? '✓ copiado' : 'copiar'}
    </button>
  </div>
{/snippet}

<div class="max-w-3xl mx-auto space-y-6">

  <div>
    <h1 class="text-xl font-semibold text-gray-100">Configuración de agentes</h1>
    <p class="text-sm text-gray-500 mt-1">
      El proxy debe estar corriendo antes de conectar cualquier agente.
      Configurá los deployments en la pestaña <span class="text-violet-400 font-medium">Deployments</span> primero.
    </p>
  </div>

  <!-- Puertos -->
  <div class="grid grid-cols-2 gap-3 text-sm">
    <div class="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 flex items-center gap-3">
      <span class="w-2 h-2 rounded-full bg-blue-400 shrink-0"></span>
      <div>
        <p class="text-gray-400 text-xs">OpenAI proxy</p>
        <code class="text-gray-200 font-mono">localhost:8787</code>
      </div>
    </div>
    <div class="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 flex items-center gap-3">
      <span class="w-2 h-2 rounded-full bg-violet-400 shrink-0"></span>
      <div>
        <p class="text-gray-400 text-xs">Anthropic proxy</p>
        <code class="text-gray-200 font-mono">localhost:8788</code>
      </div>
    </div>
  </div>

  <!-- ── CLAUDE CODE ─────────────────────────────────────────────────────── -->
  <section class="bg-gray-900 border border-violet-900/50 rounded-2xl overflow-hidden">
    <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
      <span class="text-2xl">⬡</span>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-gray-100">Claude Code</h2>
          <span class="px-2 py-0.5 rounded text-xs bg-violet-900/50 text-violet-300 border border-violet-800">Anthropic :8788</span>
        </div>
        <p class="text-xs text-gray-500 mt-0.5">Configuración por proyecto — tu plan pago global queda intacto</p>
      </div>
    </div>

    <div class="px-5 py-4 space-y-5">

      <!-- Jerarquía -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Cómo funciona la jerarquía de config</p>
        <div class="bg-gray-950 rounded-xl p-4 font-mono text-xs space-y-1.5">
          <div class="flex items-center gap-2">
            <span class="text-gray-600">1.</span>
            <code class="text-gray-500">~/.claude/settings.json</code>
            <span class="ml-auto text-red-400/70">← global (tu plan pago, NO tocar)</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-gray-600">2.</span>
            <code class="text-gray-500">{'{proyecto}'}/.claude/settings.json</code>
            <span class="ml-auto text-yellow-400/70">← compartido del equipo</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-gray-600">3.</span>
            <code class="text-green-400">{'{proyecto}'}/.claude/settings.local.json</code>
            <span class="ml-auto text-green-400/70">← vos, acá</span>
          </div>
        </div>
        <p class="text-xs text-gray-500 mt-2">
          Cada nivel sobreescribe al anterior. El archivo <code class="text-gray-400">.local.json</code> es personal
          y no se commitea — tu config global con el plan pago no se toca nunca.
        </p>
      </div>

      <!-- Paso 1 -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Paso 1 — Crear el archivo en la raíz del proyecto</p>
        <p class="text-xs text-gray-500 mb-2">Ejecutá esto desde la raíz del proyecto donde querés usar el proxy:</p>
        {@render codeblock('cc-bash', claudeCodeBash)}
        <p class="text-xs text-gray-600 mt-2">
          O crearlo a mano — contenido de <code class="text-gray-400">.claude/settings.local.json</code>:
        </p>
        {@render codeblock('cc-json', claudeCodeJson)}
      </div>

      <!-- Paso 2 -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Paso 2 — Verificar</p>
        {@render codeblock('cc-verify', claudeCodeVerify)}
        <p class="text-xs text-gray-500 mt-2">
          Si el comando responde y aparece un registro en la pestaña
          <span class="text-violet-400">Logs</span>, está funcionando.
        </p>
      </div>

      <!-- Mapeo -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Mapeo de modelos (automático)</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Claude Code pide</th>
                <th class="px-4 py-2">Proxy usa</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['claude-opus-*',   'infinity/opus',   'text-violet-400'],
                ['claude-sonnet-*', 'infinity/sonnet', 'text-blue-400'],
                ['claude-haiku-*',  'infinity/haiku',  'text-green-400'],
              ] as [from, to, cls]}
                <tr>
                  <td class="px-4 py-2.5 font-mono text-gray-400">{from}</td>
                  <td class="px-4 py-2.5 font-mono {cls}">{to}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

    </div>
  </section>

  <!-- ── ROOCODE ─────────────────────────────────────────────────────────── -->
  <section class="bg-gray-900 border border-blue-900/50 rounded-2xl overflow-hidden">
    <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
      <span class="text-2xl">🦘</span>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-gray-100">RooCode</h2>
          <span class="px-2 py-0.5 rounded text-xs bg-blue-900/50 text-blue-300 border border-blue-800">OpenAI :8787</span>
        </div>
        <p class="text-xs text-gray-500 mt-0.5">Extensión de VS Code — configuración en el panel de settings</p>
      </div>
    </div>

    <div class="px-5 py-4 space-y-5">

      <!-- Pasos -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Pasos en VS Code</p>
        <ol class="space-y-3">
          {#each [
            'Abrí el panel de RooCode desde la barra lateral o con <kbd class="bg-gray-800 px-1 rounded text-gray-300">Ctrl+Shift+R</kbd>',
            'Clic en el ícono de engranaje ⚙ (Settings) en la esquina superior derecha del panel',
            'En la sección <strong>API Provider</strong>, seleccioná <strong>OpenAI Compatible</strong>',
            'Completá los tres campos de la tabla de abajo y guardá',
          ] as step, i}
            <li class="flex gap-3 text-sm">
              <span class="shrink-0 w-5 h-5 rounded-full bg-blue-900/50 text-blue-300 text-xs flex items-center justify-center font-bold border border-blue-800">{i + 1}</span>
              <span class="text-gray-400">{@html step}</span>
            </li>
          {/each}
        </ol>
      </div>

      <!-- Campos -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Campos a completar</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Campo</th>
                <th class="px-4 py-2">Valor</th>
                <th class="px-2 py-2 w-12"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['Base URL', 'http://127.0.0.1:8787/v1', 'roo-base'],
                ['API Key',  'infinity-proxy',            'roo-key'],
                ['Model',    'infinity/sonnet',           'roo-model'],
              ] as [field, value, id]}
                <tr>
                  <td class="px-4 py-2.5 text-gray-400">{field}</td>
                  <td class="px-4 py-2.5 font-mono text-gray-200">{value}</td>
                  <td class="px-2 py-2.5">
                    <button
                      onclick={() => copy(id, value)}
                      class="text-gray-600 hover:text-gray-300 transition-colors text-xs px-2 py-1 rounded hover:bg-gray-800"
                    >{copied[id] ? '✓' : '⎘'}</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Modos -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Modelo por modo (opcional)</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Modo</th>
                <th class="px-4 py-2">Modelo</th>
                <th class="px-4 py-2">Por qué</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['Code',        'infinity/sonnet', 'text-blue-400',   'Build y ejecución de código'],
                ['Architect',   'infinity/opus',   'text-violet-400', 'Planificación y diseño'],
                ['Ask / Debug', 'infinity/haiku',  'text-green-400',  'Respuestas rápidas'],
                ['Orchestrator','infinity/opus',   'text-violet-400', 'Razonamiento complejo'],
              ] as [mode, model, cls, why]}
                <tr>
                  <td class="px-4 py-2.5 text-gray-300 font-medium">{mode}</td>
                  <td class="px-4 py-2.5 font-mono {cls}">{model}</td>
                  <td class="px-4 py-2.5 text-gray-500">{why}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Verificar -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Verificar</p>
        {@render codeblock('roo-verify', rooCodeVerify)}
      </div>

    </div>
  </section>

  <!-- ── OPENCODE ────────────────────────────────────────────────────────── -->
  <section class="bg-gray-900 border border-blue-900/50 rounded-2xl overflow-hidden">
    <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
      <span class="text-2xl">◈</span>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-gray-100">OpenCode</h2>
          <span class="px-2 py-0.5 rounded text-xs bg-blue-900/50 text-blue-300 border border-blue-800">OpenAI :8787</span>
        </div>
        <p class="text-xs text-gray-500 mt-0.5">Terminal AI — editar opencode.json</p>
      </div>
    </div>

    <div class="px-5 py-4 space-y-5">

      <!-- JSON -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
          Editar <code class="normal-case font-mono text-gray-300">~/.config/opencode/opencode.json</code>
        </p>
        <p class="text-xs text-gray-500 mb-2">
          Agregá el proveedor <code class="text-gray-400">infinity</code>. Si el archivo ya tiene contenido
          (MCPs, keybinds), hacé <strong class="text-gray-300">merge</strong> — no reemplaces todo el archivo.
        </p>
        {@render codeblock('oc-json', openCodeJson)}
      </div>

      <!-- Warning merge -->
      <div class="bg-yellow-950/30 border border-yellow-800/50 rounded-xl px-4 py-3">
        <p class="text-xs text-yellow-300 font-medium mb-1">⚠ No reemplaces el archivo completo</p>
        <p class="text-xs text-yellow-200/60">
          Solo agregá el bloque <code class="text-yellow-300">"infinity": {'{'}&hellip;{'}'}</code> dentro de la sección
          <code class="text-yellow-300">"provider"</code> existente y cambiá <code class="text-yellow-300">"model"</code>
          al valor deseado.
        </p>
      </div>

      <!-- Campos clave -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Campos clave</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Campo</th>
                <th class="px-4 py-2">Valor</th>
                <th class="px-2 py-2 w-12"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['base',           'http://127.0.0.1:8787/v1', 'oc-base'],
                ['apiKey',         'infinity-proxy',            'oc-key'],
                ['model (default)','infinity/sonnet',           'oc-model'],
              ] as [field, value, id]}
                <tr>
                  <td class="px-4 py-2.5 font-mono text-gray-400">{field}</td>
                  <td class="px-4 py-2.5 font-mono text-gray-200">{value}</td>
                  <td class="px-2 py-2.5">
                    <button
                      onclick={() => copy(id, value)}
                      class="text-gray-600 hover:text-gray-300 transition-colors text-xs px-2 py-1 rounded hover:bg-gray-800"
                    >{copied[id] ? '✓' : '⎘'}</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Verificar -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Verificar</p>
        {@render codeblock('oc-verify', openCodeVerify)}
      </div>

    </div>
  </section>

  <!-- ── MIMO CODE ───────────────────────────────────────────────────────── -->
  <section class="bg-gray-900 border border-orange-900/50 rounded-2xl overflow-hidden">
    <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
      <span class="text-2xl">Ⓜ</span>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-gray-100">MiMo Code</h2>
          <span class="px-2 py-0.5 rounded text-xs bg-orange-900/50 text-orange-300 border border-orange-800">OpenAI :8787</span>
        </div>
        <p class="text-xs text-gray-500 mt-0.5">CLI de Xiaomi — editar mimocode.jsonc</p>
      </div>
    </div>

    <div class="px-5 py-4 space-y-5">

      <!-- JSON -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
          Editar <code class="normal-case font-mono text-gray-300">~/.config/mimocode/mimocode.jsonc</code>
          (o <code class="normal-case font-mono text-gray-300">.mimocode/mimocode.jsonc</code> en el proyecto)
        </p>
        <p class="text-xs text-gray-500 mb-2">
          Agregá el proveedor <code class="text-gray-400">infinity</code>. Si el archivo ya tiene contenido,
          hacé <strong class="text-gray-300">merge</strong> — no reemplaces todo el archivo.
        </p>
        {@render codeblock('mc-json', mimoCodeJson)}
      </div>

      <!-- Campos clave -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Campos clave</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Campo</th>
                <th class="px-4 py-2">Valor</th>
                <th class="px-2 py-2 w-12"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['baseURL',         'http://127.0.0.1:8787/v1', 'mc-base'],
                ['apiKey',          'infinity-proxy',            'mc-key'],
                ['model (default)', 'infinity/sonnet',           'mc-model'],
              ] as [field, value, id]}
                <tr>
                  <td class="px-4 py-2.5 font-mono text-gray-400">{field}</td>
                  <td class="px-4 py-2.5 font-mono text-gray-200">{value}</td>
                  <td class="px-2 py-2.5">
                    <button
                      onclick={() => copy(id, value)}
                      class="text-gray-600 hover:text-gray-300 transition-colors text-xs px-2 py-1 rounded hover:bg-gray-800"
                    >{copied[id] ? '✓' : '⎘'}</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Verificar -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Verificar</p>
        {@render codeblock('mc-verify', mimoCodeVerify)}
      </div>

    </div>
  </section>

  <!-- ── HERMES AGENT ────────────────────────────────────────────────────── -->
  <section class="bg-gray-900 border border-teal-900/50 rounded-2xl overflow-hidden">
    <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
      <span class="text-2xl">☤</span>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-gray-100">Hermes Agent</h2>
          <span class="px-2 py-0.5 rounded text-xs bg-teal-900/50 text-teal-300 border border-teal-800">OpenAI :8787</span>
        </div>
        <p class="text-xs text-gray-500 mt-0.5">CLI de Nous Research — proveedor custom en config.yaml</p>
      </div>
    </div>

    <div class="px-5 py-4 space-y-5">

      <!-- YAML -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
          Editar <code class="normal-case font-mono text-gray-300">~/.hermes/config.yaml</code>
        </p>
        <p class="text-xs text-gray-500 mb-2">
          Alternativa: correr el asistente interactivo <code class="text-gray-400">hermes model</code> y elegir
          <strong class="text-gray-300">"Custom endpoint (self-hosted / VLLM / etc.)"</strong>.
        </p>
        {@render codeblock('hm-yaml', hermesConfigYaml)}
      </div>

      <!-- Campos clave -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Campos clave</p>
        <div class="rounded-xl overflow-hidden border border-gray-800 text-xs">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-gray-500">
                <th class="px-4 py-2">Campo</th>
                <th class="px-4 py-2">Valor</th>
                <th class="px-2 py-2 w-12"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800">
              {#each [
                ['base_url',       'http://127.0.0.1:8787/v1', 'hm-base'],
                ['api_key',        'infinity-proxy',            'hm-key'],
                ['default (modelo)', 'infinity/sonnet',         'hm-model'],
              ] as [field, value, id]}
                <tr>
                  <td class="px-4 py-2.5 font-mono text-gray-400">{field}</td>
                  <td class="px-4 py-2.5 font-mono text-gray-200">{value}</td>
                  <td class="px-2 py-2.5">
                    <button
                      onclick={() => copy(id, value)}
                      class="text-gray-600 hover:text-gray-300 transition-colors text-xs px-2 py-1 rounded hover:bg-gray-800"
                    >{copied[id] ? '✓' : '⎘'}</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Verificar -->
      <div>
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Verificar</p>
        {@render codeblock('hm-verify', hermesVerify)}
      </div>

    </div>
  </section>

</div>
