<script>
  import { api } from '../lib/api.js'

  // Providers cargados dinámicamente desde /api/providers (data-driven).
  let PROVIDERS = $state([])
  let providersByName = $state({})

  let instances = $state([])
  let loading = $state(true)
  let error = $state('')
  let showModal = $state(false)
  let saving = $state(false)
  let editTarget = $state(null)
  let validating = $state(false)

  const emptyForm = () => ({
    id: '', name: '', provider: 'openrouter', api_key: '', is_free: true,
  })
  let form = $state(emptyForm())

  let isOauthProvider = $derived(providersByName[form.provider]?.auth_type === 'oauth_device')

  // Flujo OAuth Device (RFC 8628) — alta o reautenticación de instancias
  // oauth_device. `reauthTarget` no-null indica que el modal está en modo
  // reautenticación de una instancia existente en vez de alta nueva.
  let reauthTarget = $state(null)
  let oauthFlow = $state(null)   // { flowId, userCode, verificationUri, status, error }
  let oauthInterval = null
  // profileArn de Kiro: no hay forma de resolverlo vía API para cuentas AWS
  // Builder ID (ver openspec/changes/add-kiro-chat-provider design.md) — el
  // usuario lo aporta a mano, obtenido una vez por fuera de esta app.
  let profileArnInput = $state('')

  function providerLabel(name) {
    return providersByName[name]?.label || name
  }

  async function startOauthLogin(providerName, instanceName, reauthInstanceId = null) {
    oauthFlow = { status: 'starting', error: '' }
    try {
      const res = reauthInstanceId
        ? await api.reauthInstance(providerName, reauthInstanceId, profileArnInput.trim())
        : await api.startOauthFlow(providerName, instanceName, profileArnInput.trim())
      oauthFlow = {
        flowId: res.flow_id, userCode: res.user_code,
        verificationUri: res.verification_uri, status: 'pending', error: '',
      }
      clearInterval(oauthInterval)
      oauthInterval = setInterval(() => pollOauth(providerName, res.flow_id), 3000)
    } catch (e) {
      oauthFlow = { status: 'error', error: e.message }
    }
  }

  async function pollOauth(providerName, flowId) {
    try {
      const res = await api.pollOauthStatus(providerName, flowId)
      if (res.status === 'complete') {
        clearInterval(oauthInterval)
        oauthFlow = { ...oauthFlow, status: 'complete' }
        await load()
        setTimeout(() => { showModal = false }, 1200)
      } else if (res.status === 'error') {
        clearInterval(oauthInterval)
        oauthFlow = { ...oauthFlow, status: 'error', error: res.message }
      }
    } catch (e) {
      clearInterval(oauthInterval)
      oauthFlow = { ...oauthFlow, status: 'error', error: e.message }
    }
  }

  function openReauth(inst) {
    error = ''
    reauthTarget = inst
    oauthFlow = null
    profileArnInput = ''
    showModal = true
    startOauthLogin(inst.provider, inst.name, inst.id)
  }

  // Models preview panel
  let modelsPanel = $state(null)   // { instanceId, provider, models, loading, error, filter }
  let modelSearch = $state('')

  let filteredPreviewModels = $derived(
    modelsPanel && modelsPanel.models
      ? (modelSearch.trim() === ''
          ? modelsPanel.models
          : modelsPanel.models.filter(m => m.toLowerCase().includes(modelSearch.toLowerCase()))
        )
      : []
  )

  async function load() {
    loading = true
    error = ''
    try {
      const [insts, provs] = await Promise.all([api.getInstances(), api.getProviders()])
      instances = insts
      PROVIDERS = provs.map(p => ({ value: p.name, label: p.label || p.name }))
      providersByName = Object.fromEntries(provs.map(p => [p.name, p]))
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  function closeModal() {
    clearInterval(oauthInterval)
    showModal = false
    reauthTarget = null
    oauthFlow = null
    profileArnInput = ''
    error = ''
  }

  function openCreate() {
    form = emptyForm()
    editTarget = null
    reauthTarget = null
    oauthFlow = null
    profileArnInput = ''
    showModal = true
  }

  function openEdit(inst) {
    form = { ...inst, api_key: '', is_free: Boolean(inst.is_free) }
    editTarget = inst.id
    reauthTarget = null
    oauthFlow = null
    showModal = true
  }

  async function save() {
    saving = true
    validating = true
    error = ''
    try {
      if (editTarget) {
        await api.updateInstance(editTarget, form)
      } else {
        await api.createInstance(form)
      }
      const savedId = editTarget || form.id
      const savedProvider = form.provider
      const isFree = form.is_free
      showModal = false
      await load()
      // Auto-open models panel after save
      await openModelsPanel(savedId, savedProvider, isFree)
    } catch (e) {
      error = e.message
    } finally {
      saving = false
      validating = false
    }
  }

  async function del(id) {
    if (!confirm(`¿Eliminar instancia '${id}'? Esto también borrará sus deployments.`)) return
    try {
      await api.deleteInstance(id)
      if (modelsPanel?.instanceId === id) modelsPanel = null
      await load()
    } catch (e) {
      error = e.message
    }
  }

  async function openModelsPanel(instanceId, provider, isFree) {
    modelsPanel = { instanceId, provider, models: [], loading: true, error: '', filter: 'auto' }
    modelSearch = ''
    try {
      const filter = isFree ? 'free' : 'all'
      const data = await api.getProviderModels(instanceId, filter)
      modelsPanel.models = data.models || []
      modelsPanel.loading = false
    } catch (e) {
      modelsPanel.error = e.message
      modelsPanel.loading = false
    }
  }

  function closeModelsPanel() {
    modelsPanel = null
    modelSearch = ''
  }

  function copyToClipboard(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
      } else {
        fallbackCopy(text)
      }
    } catch {
      fallbackCopy(text)
    }
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

  $effect(() => { load() })
</script>

<div class="max-w-5xl mx-auto space-y-4">
  <div class="flex items-center justify-between">
    <h1 class="text-xl font-semibold text-gray-100">API Keys</h1>
    <button
      onclick={openCreate}
      class="px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-sm font-medium transition-colors"
    >
      + Nueva API
    </button>
  </div>

  {#if error}
    <div class="bg-red-900/40 border border-red-700 rounded-lg px-4 py-2 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="text-gray-500 text-sm">Cargando...</div>
  {:else if instances.length === 0}
    <div class="text-center py-16 text-gray-600">
      <div class="text-4xl mb-3">🔑</div>
      <p>No hay APIs configuradas todavía.</p>
      <p class="text-sm mt-1">Agrega una para poder construir deployments.</p>
    </div>
  {:else}
    <div class="space-y-2">
      {#each instances as inst (inst.id)}
        {@const BADGE_CLS = {
          openrouter: 'bg-orange-900/50 text-orange-300 border border-orange-800',
          gemini:     'bg-blue-900/50 text-blue-300 border border-blue-800',
          groq:       'bg-green-900/50 text-green-300 border border-green-800',
          zai:        'bg-purple-900/50 text-purple-300 border border-purple-800',
          zen:        'bg-cyan-900/50 text-cyan-300 border border-cyan-800',
          kilo:       'bg-yellow-900/50 text-yellow-300 border border-yellow-800',
          nvidia:     'bg-green-900/40 text-green-300 border border-green-800',
          ollama:     'bg-gray-800/60 text-gray-300 border border-gray-700',
        }}
        {@const BADGE_LABEL = { openrouter: 'OpenRouter', gemini: 'Gemini', groq: 'Groq', zai: 'Z.AI', zen: 'Zen', kilo: 'Kilo', nvidia: 'NVIDIA', ollama: 'Ollama' }}
        <div class="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex items-center gap-4 {modelsPanel?.instanceId === inst.id ? 'border-violet-700' : ''}">
          <!-- Provider badge -->
          <span class="px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide {BADGE_CLS[inst.provider] ?? 'bg-gray-800 text-gray-300 border border-gray-700'}">
            {BADGE_LABEL[inst.provider] ?? inst.provider}
          </span>

          <!-- Info -->
          <div class="flex-1 min-w-0">
            <p class="font-medium text-gray-100 truncate">{inst.name}</p>
            <p class="text-xs text-gray-500 font-mono">{inst.id} · {inst.api_key}</p>
          </div>

          <!-- Free/paid badge -->
          <span class="px-2 py-0.5 rounded text-xs font-medium
            {inst.is_free ? 'bg-green-900/40 text-green-400 border border-green-800' : 'bg-yellow-900/40 text-yellow-400 border border-yellow-800'}">
            {inst.is_free ? 'Free' : 'Paid'}
          </span>

          <!-- OAuth needs_reauth badge -->
          {#if inst.oauth_status === 'needs_reauth'}
            <button
              onclick={() => openReauth(inst)}
              class="px-2 py-0.5 rounded text-xs font-medium bg-red-900/40 text-red-300 border border-red-800 hover:bg-red-900/60 transition-colors"
              title="El token OAuth expiró o fue revocado — reautentica esta cuenta"
            >⚠ Reautenticar</button>
          {:else if inst.oauth_status === 'needs_profile_arn'}
            <button
              onclick={() => openReauth(inst)}
              class="px-2 py-0.5 rounded text-xs font-medium bg-yellow-900/40 text-yellow-300 border border-yellow-800 hover:bg-yellow-900/60 transition-colors"
              title="Falta profileArn — sin él esta cuenta no puede enrutar chat, reautentica aportándolo"
            >⚠ Falta profileArn</button>
          {/if}

          <!-- Models button -->
          <button
            onclick={() => modelsPanel?.instanceId === inst.id ? closeModelsPanel() : openModelsPanel(inst.id, inst.provider, inst.is_free)}
            class="text-xs px-2 py-1 rounded transition-colors
              {modelsPanel?.instanceId === inst.id
                ? 'bg-violet-700 text-white'
                : 'text-gray-500 hover:text-gray-300 hover:bg-gray-800'}"
            title="Ver modelos disponibles"
          >{modelsPanel?.instanceId === inst.id ? '✕ cerrar' : 'modelos'}</button>

          <!-- Actions -->
          <button onclick={() => openEdit(inst)} class="text-gray-500 hover:text-gray-300 text-sm px-2 py-1 rounded hover:bg-gray-800 transition-colors">✏️</button>
          <button onclick={() => del(inst.id)} class="text-gray-500 hover:text-red-400 text-sm px-2 py-1 rounded hover:bg-gray-800 transition-colors">🗑️</button>
        </div>

        <!-- Models preview panel (inline under the key card) -->
        {#if modelsPanel?.instanceId === inst.id}
          <div class="bg-gray-900/60 border border-gray-800 rounded-xl px-5 py-4 space-y-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm font-medium text-gray-200">Modelos disponibles</span>
                {#if modelsPanel.loading}
                  <span class="text-xs text-gray-500 animate-pulse">cargando...</span>
                {:else}
                  <span class="text-xs text-gray-500">({filteredPreviewModels.length}{modelSearch ? ' de ' + modelsPanel.models.length : ''})</span>
                {/if}
              </div>
              <input
                bind:value={modelSearch}
                placeholder="Buscar..."
                class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-violet-500 w-56"
              />
            </div>

            {#if modelsPanel.error}
              <div class="text-xs text-red-400">{modelsPanel.error}</div>
            {:else if modelsPanel.loading}
              <div class="text-xs text-gray-500">Consultando {modelsPanel.provider}...</div>
            {:else if modelsPanel.models.length === 0}
              <div class="text-xs text-gray-500">Sin modelos disponibles</div>
            {:else}
              <div class="max-h-64 overflow-y-auto">
                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1.5">
                  {#each filteredPreviewModels.slice(0, 200) as m}
                    {@const isFree = m.endsWith(':free')}
                    <div class="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-gray-800/60 hover:bg-gray-800 transition-colors group">
                      <span class="shrink-0 w-1.5 h-1.5 rounded-full {isFree ? 'bg-green-500' : 'bg-yellow-500'}"></span>
                      <span class="text-xs font-mono text-gray-300 truncate flex-1" title="{m}">{m}</span>
                      <button
                        onclick={() => copyToClipboard(m)}
                        class="text-gray-600 hover:text-gray-300 text-xs px-1 py-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity"
                        title="Copiar"
                      >⎘</button>
                    </div>
                  {/each}
                </div>
                {#if filteredPreviewModels.length > 200}
                  <p class="text-xs text-gray-600 text-center mt-2">Mostrando 200 de {filteredPreviewModels.length} · escribe para filtrar</p>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

{#snippet oauthPanel(providerName, instanceName, reauthInstanceId)}
  {#if !oauthFlow}
    {#if providerName === 'kiro'}
      <label class="block">
        <span class="text-xs text-gray-400 mb-1 block">
          profileArn (opcional — sin él, la cuenta queda sin poder chatear hasta reautenticar)
        </span>
        <input
          bind:value={profileArnInput}
          placeholder="arn:aws:codewhisperer:us-east-1:...:profile/..."
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs font-mono text-gray-100 focus:outline-none focus:border-violet-500 mb-3"
        />
      </label>
    {/if}
    <button
      type="button"
      onclick={() => startOauthLogin(providerName, instanceName, reauthInstanceId)}
      class="w-full py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-sm font-medium transition-colors"
    >
      Iniciar sesión con {providerLabel(providerName)}
    </button>
  {:else if oauthFlow.status === 'starting'}
    <div class="flex items-center gap-2 text-sm text-gray-400">
      <span class="w-3 h-3 border-2 border-gray-500/30 border-t-gray-300 rounded-full animate-spin"></span>
      Iniciando sesión...
    </div>
  {:else if oauthFlow.status === 'pending'}
    <div class="space-y-2 text-sm">
      <p class="text-gray-400">Abre esta URL y autoriza con el código:</p>
      <div class="flex items-center gap-2">
        <a href={oauthFlow.verificationUri} target="_blank" rel="noopener noreferrer" class="text-violet-400 underline truncate">{oauthFlow.verificationUri}</a>
        <button type="button" onclick={() => copyToClipboard(oauthFlow.verificationUri)} class="text-gray-500 hover:text-gray-300 text-xs shrink-0">⎘</button>
      </div>
      <div class="flex items-center gap-2">
        <span class="font-mono text-lg tracking-widest bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5">{oauthFlow.userCode}</span>
        <button type="button" onclick={() => copyToClipboard(oauthFlow.userCode)} class="text-xs text-gray-500 hover:text-gray-300">⎘ copiar</button>
      </div>
      <div class="flex items-center gap-2 text-gray-500">
        <span class="w-3 h-3 border-2 border-gray-500/30 border-t-gray-300 rounded-full animate-spin"></span>
        Esperando autorización...
      </div>
    </div>
  {:else if oauthFlow.status === 'complete'}
    <div class="text-sm text-green-400">✓ Autorizado correctamente</div>
  {:else if oauthFlow.status === 'error'}
    <div class="space-y-2">
      <div class="text-sm text-red-400">{oauthFlow.error}</div>
      <button type="button" onclick={() => { oauthFlow = null }} class="text-xs text-violet-400 hover:underline">Reintentar</button>
    </div>
  {/if}
{/snippet}

<!-- Modal -->
{#if showModal}
  <div class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
    <div class="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-md p-6 space-y-4">
      <h2 class="text-lg font-semibold">
        {reauthTarget ? `Reautenticar '${reauthTarget.name}'` : (editTarget ? 'Editar API' : 'Nueva API')}
      </h2>

      {#if error}
        <div class="bg-red-900/40 border border-red-700 rounded-lg px-3 py-2 text-red-300 text-sm">{error}</div>
      {/if}

      {#if reauthTarget}
        {@render oauthPanel(reauthTarget.provider, reauthTarget.name, reauthTarget.id)}
      {:else}
        <div class="space-y-3">
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">ID</span>
            <input
              bind:value={form.id}
              disabled={!!editTarget}
              placeholder="apifree1"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 disabled:opacity-50 font-mono"
            />
          </label>

          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Nombre legible</span>
            <input
              bind:value={form.name}
              placeholder="Mi OpenRouter Free"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
            />
          </label>

          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Proveedor</span>
            <select
              bind:value={form.provider}
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
            >
              {#each PROVIDERS as p}
                <option value={p.value}>{p.label}</option>
              {/each}
            </select>
          </label>

          {#if isOauthProvider}
            {#if !editTarget}
              {@render oauthPanel(form.provider, form.name, null)}
            {:else}
              <div class="text-xs text-gray-500 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2">
                Cuenta gestionada por OAuth — usa "Reautenticar" desde la lista si el token expiró.
              </div>
            {/if}
          {:else}
            <label class="block">
              <span class="text-xs text-gray-400 mb-1 block">API Key {editTarget ? '(dejar vacío para no cambiar)' : ''}</span>
              <input
                bind:value={form.api_key}
                type="password"
                placeholder="sk-or-v1-..."
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono"
              />
            </label>
          {/if}

          <button
            type="button"
            onclick={() => (form.is_free = !form.is_free)}
            class="flex items-center gap-3 cursor-pointer bg-transparent border-none p-0"
            aria-label="Tipo de plan"
          >
            <div
              class="w-10 h-6 rounded-full transition-colors relative
                {form.is_free ? 'bg-green-600' : 'bg-yellow-600'}"
            >
              <span class="absolute top-1 w-4 h-4 bg-white rounded-full transition-all
                {form.is_free ? 'left-1' : 'left-5'}"></span>
            </div>
            <span class="text-sm text-gray-200">{form.is_free ? 'Free tier' : 'Paid'}</span>
          </button>
        </div>
      {/if}

      <div class="flex gap-3 pt-2">
        {#if !reauthTarget && !(isOauthProvider && !editTarget)}
          <button
            onclick={save}
            disabled={saving}
            class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors flex items-center justify-center gap-2"
          >
            {#if validating}
              <span class="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin"></span>
              Verificando key...
            {:else if saving}
              Guardando...
            {:else}
              Guardar
            {/if}
          </button>
        {/if}
        <button
          onclick={closeModal}
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
        >
          {reauthTarget || (isOauthProvider && !editTarget) ? 'Cerrar' : 'Cancelar'}
        </button>
      </div>
    </div>
  </div>
{/if}
