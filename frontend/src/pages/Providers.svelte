<script>
  import { api } from '../lib/api.js'

  const AUTH_TYPES = [
    { value: 'bearer',  label: 'Bearer (usa la API key de la instancia)' },
    { value: 'keyless', label: 'Keyless (sin Authorization)' },
    { value: 'static',  label: 'Static (valor fijo, ej. "Bearer anonymous")' },
    { value: 'oauth_device', label: 'OAuth Device Flow (RFC 8628, ej. Kiro)' },
  ]

  let providers = $state([])
  let loading = $state(true)
  let error = $state('')
  let showModal = $state(false)
  let saving = $state(false)
  let editTarget = $state(null)   // name original si se edita
  let headersText = $state('{}')  // extra_headers como texto JSON

  const emptyForm = () => ({
    name: '', label: '', base_url: '', models_url: '',
    auth_type: 'bearer', auth_value: '', extra_headers: {}, kind: 'plain',
  })
  let form = $state(emptyForm())

  async function load() {
    loading = true; error = ''
    try {
      providers = await api.getProviders()
    } catch (e) { error = e.message } finally { loading = false }
  }

  function openCreate() {
    form = emptyForm()
    headersText = '{}'
    editTarget = null
    error = ''
    showModal = true
  }

  function openEdit(p) {
    form = { ...p, extra_headers: p.extra_headers || {} }
    headersText = JSON.stringify(p.extra_headers || {}, null, 2)
    editTarget = p.name
    error = ''
    showModal = true
  }

  async function save() {
    error = ''
    // Validar JSON de headers
    let parsedHeaders
    try {
      parsedHeaders = headersText.trim() ? JSON.parse(headersText) : {}
    } catch {
      error = 'extra_headers no es JSON válido'
      return
    }
    const needsModelsUrl = form.auth_type !== 'oauth_device'
    if (!form.name.trim() || !form.base_url.trim() || (needsModelsUrl && !form.models_url.trim())) {
      error = needsModelsUrl
        ? 'name, base_url y models_url son obligatorios'
        : 'name y base_url son obligatorios'
      return
    }
    saving = true
    try {
      const payload = { ...form, extra_headers: parsedHeaders }
      if (editTarget) {
        await api.updateProvider(editTarget, payload)
      } else {
        await api.createProvider(payload)
      }
      showModal = false
      await load()
    } catch (e) { error = e.message } finally { saving = false }
  }

  async function del(name) {
    if (!confirm(`¿Eliminar provider '${name}'?`)) return
    try {
      await api.deleteProvider(name)
      await load()
    } catch (e) { error = e.message }
  }

  $effect(() => { load() })
</script>

<div class="max-w-5xl mx-auto space-y-4">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-semibold text-gray-100">Providers</h1>
      <p class="text-xs text-gray-500 mt-0.5">URLs, auth y headers editables. Renombrar propaga en cascada a instancias y deployments.</p>
    </div>
    <button onclick={openCreate}
      class="px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-sm font-medium transition-colors">
      + Nuevo Provider
    </button>
  </div>

  {#if error}
    <div class="bg-red-900/40 border border-red-700 rounded-lg px-4 py-2 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="text-gray-500 text-sm">Cargando...</div>
  {:else if providers.length === 0}
    <div class="text-center py-16 text-gray-600">
      <div class="text-4xl mb-3">🔌</div>
      <p>No hay providers configurados.</p>
    </div>
  {:else}
    <div class="space-y-2">
      {#each providers as p (p.name)}
        <div class="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex items-center gap-4">
          <span class="px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide bg-violet-900/40 text-violet-300 border border-violet-800">
            {p.name}
          </span>
          <div class="flex-1 min-w-0">
            <p class="font-medium text-gray-100 truncate">{p.label || p.name}</p>
            <p class="text-xs text-gray-500 font-mono truncate">{p.base_url}</p>
          </div>
          <span class="px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-300 border border-gray-700" title="auth">
            {p.auth_type}
          </span>
          <span class="px-2 py-0.5 rounded text-xs font-medium bg-gray-800/60 text-gray-400 border border-gray-700" title="kind (parseo /models)">
            {p.kind}
          </span>
          <button onclick={() => openEdit(p)} class="text-gray-500 hover:text-gray-300 text-sm px-2 py-1 rounded hover:bg-gray-800 transition-colors">✏️</button>
          <button onclick={() => del(p.name)} class="text-gray-500 hover:text-red-400 text-sm px-2 py-1 rounded hover:bg-gray-800 transition-colors">🗑️</button>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <div class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
    <div class="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-lg p-6 space-y-4 max-h-[90vh] overflow-y-auto">
      <h2 class="text-lg font-semibold">{editTarget ? `Editar '${editTarget}'` : 'Nuevo Provider'}</h2>

      {#if error}
        <div class="bg-red-900/40 border border-red-700 rounded-lg px-3 py-2 text-red-300 text-sm">{error}</div>
      {/if}

      <div class="space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Nombre (identificador)</span>
            <input bind:value={form.name} placeholder="mimo"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono" />
          </label>
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Label (UI)</span>
            <input bind:value={form.label} placeholder="Mimo Code"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500" />
          </label>
        </div>

        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">Base URL</span>
          <input bind:value={form.base_url} placeholder="https://api.mimo.ai/v1"
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono" />
        </label>

        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">
            Models URL{form.auth_type === 'oauth_device' ? ' (opcional)' : ''}
          </span>
          <input bind:value={form.models_url} placeholder="https://api.mimo.ai/v1/models"
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono" />
        </label>

        <div class="grid grid-cols-2 gap-3">
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Auth type</span>
            <select bind:value={form.auth_type}
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500">
              {#each AUTH_TYPES as a}<option value={a.value}>{a.label}</option>{/each}
            </select>
          </label>
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Kind (parseo /models)</span>
            <input bind:value={form.kind} placeholder="plain"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono" />
          </label>
        </div>

        {#if form.auth_type === 'static'}
          <label class="block">
            <span class="text-xs text-gray-400 mb-1 block">Auth value (header Authorization literal)</span>
            <input bind:value={form.auth_value} placeholder="Bearer anonymous"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500 font-mono" />
          </label>
        {:else if form.auth_type === 'oauth_device'}
          <div class="text-xs text-gray-500 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2">
            Sin API key fija: cada instancia de este provider se autoriza desde la
            pestaña "API Keys" mediante un flujo de dispositivo (login en el navegador).
          </div>
        {/if}

        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">Extra headers (JSON)</span>
          <textarea bind:value={headersText} rows="3" placeholder={'{"HTTP-Referer": "http://localhost:8787"}'}
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-gray-100 focus:outline-none focus:border-violet-500 font-mono"></textarea>
        </label>
      </div>

      <div class="flex gap-3 pt-2">
        <button onclick={save} disabled={saving}
          class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors">
          {saving ? 'Guardando...' : 'Guardar'}
        </button>
        <button onclick={() => { showModal = false; error = '' }}
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors">
          Cancelar
        </button>
      </div>
    </div>
  </div>
{/if}
