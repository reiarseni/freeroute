<script>
  import { onMount } from 'svelte'
  import Sortable from 'sortablejs'
  import { api } from '../lib/api.js'

  function modelFromUrl() {
    const parts = window.location.pathname.replace(/^\//, '').split('/')
    return parts[0] === 'deployments' && parts[1] ? decodeURIComponent(parts[1]) : null
  }

  function setUrl(name, replace = false) {
    const path = '/deployments/' + encodeURIComponent(name)
    if (window.location.pathname === path) return
    history[replace ? 'replaceState' : 'pushState']({}, '', path)
  }

  let modelNames = $state([])        // [{model_name, count}]
  let selected = $state(modelFromUrl())  // model_name string
  let deployments = $state([])       // deployments del model_name seleccionado
  let instances = $state([])
  let loading = $state(true)
  let saving = $state(false)
  let error = $state('')

  // Toggle "enrutar por menor RPM" — por model_name, guardado en router_settings.least_rpm_models
  let leastRpmModels = $state({})
  let leastRpmSaving = $state(false)
  let leastRpmEnabled = $derived(Boolean(selected && leastRpmModels[selected]))

  async function toggleLeastRpm() {
    if (!selected) return
    const next = { ...leastRpmModels, [selected]: !leastRpmEnabled }
    if (!next[selected]) delete next[selected]
    leastRpmSaving = true
    try {
      await api.updateRouterSetting('least_rpm_models', next)
      leastRpmModels = next
    } catch (e) {
      error = e.message
    } finally {
      leastRpmSaving = false
    }
  }

  // Modal añadir/editar deployment
  let addModal = $state(false)
  let editingId = $state(null)   // null = modo "añadir", id = modo "editar"
  let addForm = $state({ instanceId: '', model: '', weight: 1.0, maxInputTokens: 0 })
  let availableModels = $state([])
  let loadingModels = $state(false)
  let isFreeInstance = $state(true)
  let modelFilter = $state('auto')

  // Modal crear model_name
  let newModelModal = $state(false)
  let newModelName = $state('')

  // Combobox de búsqueda de modelos
  let modelSearch = $state('')
  let modelDropdownOpen = $state(false)

  let filteredModels = $derived(
    modelSearch.trim() === ''
      ? availableModels.slice(0, 40)
      : availableModels
          .filter(m => m.toLowerCase().includes(modelSearch.toLowerCase()))
          .slice(0, 40)
  )

  async function load() {
    loading = true
    try {
      const [names, insts, settings] = await Promise.all([
        api.getModelNames(),
        api.getInstances(),
        api.getRouterSettings(),
      ])
      modelNames = names
      instances = insts
      leastRpmModels = settings.least_rpm_models ?? {}
      // Seleccionar según la URL si es válida, si no el primero disponible
      if (selected && !names.find(m => m.model_name === selected)) {
        selected = names.length > 0 ? names[0].model_name : null
      } else if (!selected && names.length > 0) {
        selected = names[0].model_name
      }
      if (selected) {
        setUrl(selected, true)
        await loadDeployments(selected)
      }
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  async function loadDeployments(modelName) {
    try {
      deployments = await api.getDeployments(modelName)
    } catch (e) {
      error = e.message
      deployments = []
    }
  }

  async function selectModel(name) {
    selected = name
    setUrl(name)
    await loadDeployments(name)
  }

  async function saveOrder() {
    saving = true
    error = ''
    try {
      // Paso 1: mover todos a órdenes negativos temporales para evitar
      // choques con el UNIQUE(model_name, order) mientras se reordena.
      for (let i = 0; i < deployments.length; i++) {
        const d = deployments[i]
        await api.updateDeployment(d.id, {
          model_name: d.model_name,
          provider: d.provider ?? '',
          api_instance_id: d.api_instance_id,
          model_id: d.model_id,
          weight: d.weight ?? 1.0,
          rpm: d.rpm ?? 0,
          tpm: d.tpm ?? 0,
          max_input_tokens: d.max_input_tokens ?? 0,
          max_parallel_requests: d.max_parallel_requests ?? 0,
          order: -(i + 1),
          enabled: Boolean(d.enabled),
        })
      }
      // Paso 2: asignar los órdenes finales
      for (let i = 0; i < deployments.length; i++) {
        const d = deployments[i]
        await api.updateDeployment(d.id, {
          model_name: d.model_name,
          provider: d.provider ?? '',
          api_instance_id: d.api_instance_id,
          model_id: d.model_id,
          weight: d.weight ?? 1.0,
          rpm: d.rpm ?? 0,
          tpm: d.tpm ?? 0,
          max_input_tokens: d.max_input_tokens ?? 0,
          max_parallel_requests: d.max_parallel_requests ?? 0,
          order: i,
          enabled: Boolean(d.enabled),
        })
      }
    } catch (e) {
      error = e.message
    } finally {
      saving = false
    }
  }

  async function toggleEnabled(d) {
    const newEnabled = !Boolean(d.enabled)
    try {
      await api.updateDeployment(d.id, {
        model_name: d.model_name,
        provider: d.provider ?? '',
        api_instance_id: d.api_instance_id,
        model_id: d.model_id,
        weight: d.weight ?? 1.0,
        rpm: d.rpm ?? 0,
        tpm: d.tpm ?? 0,
        max_input_tokens: d.max_input_tokens ?? 0,
        max_parallel_requests: d.max_parallel_requests ?? 0,
        order: d.order ?? 0,
        enabled: newEnabled,
      })
      d.enabled = newEnabled ? 1 : 0
    } catch (e) {
      error = e.message
    }
  }

  async function removeDeployment(d) {
    if (!confirm(`¿Eliminar deployment '${d.model_id}' de ${d.model_name}?`)) return
    try {
      await api.deleteDeployment(d.id)
      deployments = deployments.filter(x => x.id !== d.id)
      // Refresh count
      modelNames = await api.getModelNames()
      if (modelNames.length === 0) {
        selected = null
        deployments = []
      } else if (!modelNames.find(m => m.model_name === selected)) {
        selected = modelNames[0].model_name
        await loadDeployments(selected)
      }
    } catch (e) {
      error = e.message
    }
  }

  function openAddModal() {
    addModal = true
    editingId = null
    addForm = {
      instanceId: instances[0]?.id ?? '',
      model: '',
      weight: 1.0,
      maxInputTokens: 0,
      maxParallelRequests: 0,
    }
    availableModels = []
    modelFilter = 'auto'
    modelSearch = ''
    modelDropdownOpen = false
    const inst = instances.find(i => i.id === addForm.instanceId)
    isFreeInstance = inst ? Boolean(inst.is_free) : true
    if (addForm.instanceId) fetchModels(addForm.instanceId, 'auto')
  }

  async function openEditModal(d) {
    addModal = true
    editingId = d.id
    addForm = {
      instanceId: d.api_instance_id,
      model: d.model_id,
      weight: d.weight ?? 1.0,
      maxInputTokens: d.max_input_tokens ?? 0,
      maxParallelRequests: d.max_parallel_requests ?? 0,
    }
    modelFilter = 'auto'
    modelDropdownOpen = false
    const inst = instances.find(i => i.id === addForm.instanceId)
    isFreeInstance = inst ? Boolean(inst.is_free) : true
    if (addForm.instanceId) {
      await fetchModels(addForm.instanceId, 'auto')
      // fetchModels limpia addForm.model / modelSearch — los restauramos
      addForm.model = d.model_id
      modelSearch = d.model_id
    }
  }

  async function fetchModels(instanceId, filter = 'auto') {
    if (!instanceId) return
    loadingModels = true
    availableModels = []
    modelSearch = ''
    addForm.model = ''
    try {
      const data = await api.getProviderModels(instanceId, filter)
      isFreeInstance = data.is_free_instance ?? true
      availableModels = data.models
    } catch (e) {
      availableModels = []
    } finally {
      loadingModels = false
    }
  }

  function selectModelOption(m) {
    addForm.model = m
    modelSearch = m
    modelDropdownOpen = false
  }

  function onModelInput() {
    modelDropdownOpen = true
    if (modelSearch !== addForm.model) addForm.model = ''
  }

  function onModelBlur() {
    setTimeout(() => { modelDropdownOpen = false }, 150)
  }

  function changeFilter(f) {
    modelFilter = f
    fetchModels(addForm.instanceId, f)
  }

  async function confirmAdd() {
    if (!addForm.instanceId || !addForm.model || !selected) return
    const inst = instances.find(i => i.id === addForm.instanceId)

    if (editingId !== null) {
      const existing = deployments.find(d => d.id === editingId)
      const updated = {
        model_name: selected,
        provider: inst?.provider ?? '',
        api_instance_id: addForm.instanceId,
        model_id: addForm.model,
        weight: addForm.weight,
        rpm: existing?.rpm ?? 0,
        tpm: existing?.tpm ?? 0,
        max_input_tokens: addForm.maxInputTokens,
        max_parallel_requests: addForm.maxParallelRequests,
        order: existing?.order ?? 0,
        enabled: Boolean(existing?.enabled),
      }
      saving = true
      try {
        await api.updateDeployment(editingId, updated)
        addModal = false
        editingId = null
        deployments = await api.getDeployments(selected)
        error = ''
      } catch (e) {
        error = e.message
      } finally {
        saving = false
      }
      return
    }

    const newDep = {
      model_name: selected,
      provider: inst?.provider ?? '',
      api_instance_id: addForm.instanceId,
      model_id: addForm.model,
      weight: addForm.weight,
      rpm: 0,
      tpm: 0,
      max_input_tokens: addForm.maxInputTokens,
      max_parallel_requests: addForm.maxParallelRequests,
      order: deployments.length,  // al final
      enabled: true,
    }
    try {
      const r = await api.createDeployment(newDep)
      addModal = false
      deployments = await api.getDeployments(selected)
      modelNames = await api.getModelNames()
    } catch (e) {
      error = e.message
    }
  }

  function openNewModelModal() {
    newModelModal = true
    newModelName = ''
  }

  async function confirmCreateModel() {
    const name = newModelName.trim()
    if (!name) return
    // Validar que no exista
    if (modelNames.find(m => m.model_name === name)) {
      error = `Ya existe el model_name '${name}'`
      return
    }
    // Crear model_name seleccionándolo (no hay endpoint para crear vacío —
    // simplemente lo seleccionamos; si el usuario añadirá deployments se crearán)
    selected = name
    setUrl(name)
    deployments = []
    newModelModal = false
    // Como no hay deployments todavía, no aparece en la lista hasta que se cree uno
    // Refrescar la lista para mostrarlo si se crea el primer deployment
  }

  // Drag-and-drop
  let listEl

  function initSortable(el) {
    Sortable.create(el, {
      animation: 150,
      ghostClass: 'opacity-30',
      onEnd(evt) {
        const arr = [...deployments]
        const [moved] = arr.splice(evt.oldIndex, 1)
        arr.splice(evt.newIndex, 0, moved)
        deployments = arr
      },
    })
  }

  function providerBadge(provider) {
    return {
      openrouter: 'bg-orange-900/50 text-orange-300',
      gemini:     'bg-blue-900/50 text-blue-300',
      groq:       'bg-green-900/50 text-green-300',
      zai:        'bg-purple-900/50 text-purple-300',
      zen:        'bg-cyan-900/50 text-cyan-300',
      kilo:       'bg-yellow-900/50 text-yellow-300',
      nvidia:     'bg-green-900/40 text-green-300 border border-green-800',
      ollama:     'bg-gray-800/60 text-gray-300 border border-gray-700',
      'ollama-local': 'bg-gray-800/60 text-gray-300 border border-gray-700',
    }[provider] ?? 'bg-gray-900/50 text-gray-300'
  }

  function providerLabel(provider) {
    return {
      openrouter: 'OR',
      gemini: 'GEM',
      groq: 'GRQ',
      zai: 'ZAI',
      zen: 'ZEN',
      kilo: 'KILO',
      nvidia: 'NV',
      ollama: 'OLL',
      'ollama-local': 'OLL-L',
    }[provider] ?? provider
  }

  $effect(() => { load() })

  $effect(() => {
    function onPop() {
      const name = modelFromUrl()
      if (name && name !== selected) {
        selected = name
        loadDeployments(name)
      }
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  })
</script>

<div class="flex gap-6">
  <!-- Sidebar: lista de model_names -->
  <aside class="w-64 shrink-0">
    <div class="flex items-center justify-between mb-3">
      <h1 class="text-lg font-semibold text-gray-100">Deployments</h1>
      <button
        onclick={openNewModelModal}
        class="px-2 py-1 text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-md transition-colors"
        title="Nuevo model_name"
      >+ Model</button>
    </div>
    <p class="text-xs text-gray-500 mb-3">Rutas <code class="text-gray-400">infinity/{"<name>"}</code></p>

    {#if error}
      <div class="bg-red-900/40 border border-red-700 rounded-lg px-3 py-2 text-red-300 text-xs mb-3">{error}</div>
    {/if}

    {#if loading}
      <div class="text-gray-500 text-sm">Cargando...</div>
    {:else if modelNames.length === 0 && !selected}
      <div class="text-center py-8 text-gray-600 text-sm">
        No hay deployments todavía.<br>
        Creá un model_name y añadí deployments.
      </div>
    {:else}
      <div class="space-y-1">
        {#each modelNames as mn (mn.model_name)}
          <button
            onclick={() => selectModel(mn.model_name)}
            class="w-full text-left px-3 py-2 rounded-lg text-sm transition-colors
              {selected === mn.model_name
                ? 'bg-violet-600 text-white'
                : 'text-gray-300 hover:bg-gray-800'}"
          >
            <span class="font-mono">{mn.model_name}</span>
            <span class="ml-2 text-xs opacity-60">({mn.count})</span>
          </button>
        {/each}

        {#if selected && !modelNames.find(m => m.model_name === selected)}
          <button class="w-full text-left px-3 py-2 rounded-lg text-sm bg-violet-600/40 text-violet-200">
            <span class="font-mono">{selected}</span>
            <span class="ml-2 text-xs opacity-60">(0)</span>
          </button>
        {/if}
      </div>
    {/if}
  </aside>

  <!-- Panel derecho: deployments del model_name seleccionado -->
  <main class="flex-1 min-w-0 space-y-4">
    {#if !selected}
      <div class="text-center py-16 text-gray-600">
        <div class="text-4xl mb-3">∞</div>
        <p>Seleccioná o creá un model_name para ver sus deployments</p>
      </div>
    {:else}
      <div class="flex items-center justify-between">
        <div>
          <h2 class="text-lg font-semibold text-gray-100 font-mono">{selected}</h2>
          <p class="text-xs text-gray-500">
            {deployments.length} deployment{deployments.length !== 1 ? 's' : ''} ·
            Arrastrá para reordenar · Guardá para persistir el orden
          </p>
        </div>
        <div class="flex gap-2">
          <button
            onclick={openAddModal}
            disabled={instances.length === 0}
            class="px-3 py-1.5 bg-violet-700 hover:bg-violet-600 disabled:opacity-50 text-white rounded-lg text-xs font-medium transition-colors"
          >+ Añadir deployment</button>
          <button
            onclick={saveOrder}
            disabled={saving || deployments.length === 0}
            class="px-3 py-1.5 w-[110px] text-center text-white rounded-lg text-xs font-medium transition-colors
              {saving
                ? 'bg-amber-600'
                : deployments.length === 0
                  ? 'bg-gray-700 opacity-50'
                  : 'bg-gray-700 hover:bg-gray-600'}"
          >Guardar orden</button>
        </div>
      </div>

      <div class="flex items-center justify-between bg-gray-900 border border-gray-800 rounded-xl px-3 py-2.5">
        <div class="min-w-0">
          <p class="text-xs text-gray-300 font-medium">Enrutar por menor RPM</p>
          <p class="text-xs text-gray-500">
            Cada petición va al deployment con menos RPM consumido en el minuto actual,
            en vez del orden/estrategia global. Cooldown y límites RPM/TPM se siguen aplicando igual.
          </p>
        </div>
        <button
          onclick={toggleLeastRpm}
          disabled={leastRpmSaving}
          class="text-xs px-2 py-0.5 rounded font-medium shrink-0 transition-colors disabled:opacity-50
            {leastRpmEnabled
              ? 'bg-green-900/40 text-green-300 hover:bg-green-900/60'
              : 'bg-gray-800 text-gray-500 hover:bg-gray-700'}"
        >{leastRpmEnabled ? 'ON' : 'OFF'}</button>
      </div>

      {#if deployments.length === 0}
        <div class="text-center py-12 text-gray-600 text-sm border border-dashed border-gray-800 rounded-xl">
          Sin deployments en <span class="font-mono text-gray-400">{selected}</span>·
          Añadí uno
        </div>
      {:else}
        <div use:initSortable class="space-y-2">
          {#each deployments as d, i (d.id)}
            <div class="bg-gray-900 border border-gray-800 rounded-xl px-3 py-2.5 flex items-center gap-3 cursor-grab active:cursor-grabbing group hover:border-gray-700 transition-colors">
              <span class="text-gray-600 group-hover:text-gray-400 select-none text-xs">⠿</span>
              <span class="text-xs text-gray-600 w-4 shrink-0">{i + 1}</span>

              <span class="px-1.5 py-0.5 rounded text-xs font-medium uppercase {providerBadge(d.provider)}">
                {providerLabel(d.provider)}
              </span>

              <div class="flex-1 min-w-0">
                <p class="text-xs text-gray-300 font-medium truncate">{d.api_instance_id}</p>
                <p class="text-xs text-gray-500 font-mono truncate">{d.model_id}</p>
              </div>

              {#if d.weight !== 1.0}
                <span class="text-xs text-gray-500 shrink-0" title="Weight">w={d.weight}</span>
              {/if}
              {#if d.max_input_tokens > 0}
                <span class="text-xs text-blue-400/60 shrink-0" title="Max input tokens">↧{d.max_input_tokens}</span>
              {/if}
              {#if d.max_parallel_requests > 0}
                <span class="text-xs text-amber-400/70 shrink-0" title="Máx. peticiones en paralelo">⇉{d.max_parallel_requests}</span>
              {/if}

              <button
                onclick={() => toggleEnabled(d)}
                class="text-xs px-2 py-0.5 rounded font-medium shrink-0 transition-colors
                  {d.enabled
                    ? 'bg-green-900/40 text-green-300 hover:bg-green-900/60'
                    : 'bg-gray-800 text-gray-500 hover:bg-gray-700'}"
              >{d.enabled ? 'ON' : 'OFF'}</button>

              <button
                onclick={() => openEditModal(d)}
                class="text-gray-600 hover:text-violet-400 transition-colors shrink-0 opacity-0 group-hover:opacity-100"
                title="Editar"
              >✎</button>

              <button
                onclick={() => removeDeployment(d)}
                class="text-gray-600 hover:text-red-400 transition-colors shrink-0 opacity-0 group-hover:opacity-100"
                title="Eliminar"
              >✕</button>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
  </main>
</div>

<!-- Modal: añadir deployment -->
{#if addModal}
  <div class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
    <div class="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-sm p-6 space-y-4">
      <h2 class="text-base font-semibold">
        {#if editingId !== null}
          Editar deployment de <span class="text-violet-400 font-mono">{selected}</span>
        {:else}
          Añadir deployment a <span class="text-violet-400 font-mono">{selected}</span>
        {/if}
      </h2>

      <div class="space-y-3">
        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">API Instance</span>
          <select
            bind:value={addForm.instanceId}
            onchange={() => {
              const inst = instances.find(i => i.id === addForm.instanceId)
              isFreeInstance = inst ? Boolean(inst.is_free) : true
              fetchModels(addForm.instanceId, 'auto')
            }}
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
          >
            {#each instances as inst}
              <option value={inst.id}>{inst.name} ({inst.provider}) · {inst.is_free ? 'free' : 'paid'}</option>
            {/each}
          </select>
        </label>

        {#if !isFreeInstance}
          <div class="flex items-center gap-1 bg-gray-800 rounded-lg p-1">
            {#each [['all', 'Todos'], ['free', 'Free'], ['paid', 'Paid']] as [val, label]}
              <button
                type="button"
                onclick={() => changeFilter(val)}
                class="flex-1 py-1 text-xs rounded-md font-medium transition-colors
                  {modelFilter === val
                    ? 'bg-violet-600 text-white'
                    : 'text-gray-400 hover:text-gray-200'}"
              >
                {label}
              </button>
            {/each}
          </div>
        {/if}

        <!-- Combobox de modelos -->
        <div class="block">
          <span class="text-xs text-gray-400 mb-1 flex items-center gap-2">
            Modelo
            {#if loadingModels}
              <span class="text-gray-600 animate-pulse">cargando...</span>
            {:else if availableModels.length > 0}
              <span class="text-gray-600">{availableModels.length} disponibles</span>
            {/if}
          </span>

          <div class="relative">
            <input
              bind:value={modelSearch}
              oninput={onModelInput}
              onfocus={() => (modelDropdownOpen = true)}
              onblur={onModelBlur}
              disabled={loadingModels}
              placeholder={loadingModels ? 'Cargando...' : availableModels.length > 0 ? 'Buscar modelo...' : 'ej: minimax/minimax-m2.5:free'}
              class="w-full bg-gray-800 border rounded-lg px-3 py-2 pr-8 text-sm font-mono text-gray-100 focus:outline-none transition-colors disabled:opacity-50
                {addForm.model ? 'border-violet-500 text-violet-300' : 'border-gray-700 focus:border-violet-500'}"
            />
            <span class="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none text-xs">
              {#if addForm.model}✓{:else}↕{/if}
            </span>

            {#if modelDropdownOpen && !loadingModels && filteredModels.length > 0}
              <div class="absolute z-10 mt-1 w-full bg-gray-800 border border-gray-600 rounded-xl shadow-2xl overflow-hidden">
                {#if modelSearch.trim() !== ''}
                  <div class="px-3 py-1.5 text-xs text-gray-500 border-b border-gray-700">
                    {filteredModels.length} resultado{filteredModels.length !== 1 ? 's' : ''} para "{modelSearch}"
                  </div>
                {/if}
                <ul class="max-h-48 overflow-y-auto">
                  {#each filteredModels as m}
                    {@const isFree = m.endsWith(':free')}
                    {@const isSelected = addForm.model === m}
                    <li>
                      <button
                        type="button"
                        onmousedown={() => selectModelOption(m)}
                        class="w-full text-left px-3 py-2 text-xs font-mono flex items-center gap-2 transition-colors
                          {isSelected
                            ? 'bg-violet-600/30 text-violet-300'
                            : 'text-gray-300 hover:bg-gray-700'}"
                      >
                        <span class="shrink-0 w-1.5 h-1.5 rounded-full {isFree ? 'bg-green-500' : 'bg-yellow-500'}"></span>
                        <span class="truncate">{m}</span>
                        {#if isSelected}
                          <span class="ml-auto text-violet-400 shrink-0">✓</span>
                        {/if}
                      </button>
                    </li>
                  {/each}
                </ul>
                {#if availableModels.length > 40 && filteredModels.length === 40}
                  <div class="px-3 py-1.5 text-xs text-gray-600 border-t border-gray-700 text-center">
                    Mostrando 40 de {availableModels.length} · escribí para filtrar
                  </div>
                {/if}
              </div>
            {/if}
          </div>
        </div>

        <!-- Weight -->
        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">Weight (default 1.0)</span>
          <input
            type="number"
            step="0.1"
            min="0"
            bind:value={addForm.weight}
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
          />
        </label>

        <!-- Max input tokens -->
        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">Max input tokens (0 = sin chequeo)</span>
          <input
            type="number"
            min="0"
            bind:value={addForm.maxInputTokens}
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
          />
        </label>

        <!-- Max parallel requests -->
        <label class="block">
          <span class="text-xs text-gray-400 mb-1 block">Máx. peticiones en paralelo (0 = sin límite)</span>
          <input
            type="number"
            min="0"
            bind:value={addForm.maxParallelRequests}
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-violet-500"
          />
          <span class="text-xs text-gray-500 mt-1 block">Encola las peticiones por encima del tope en vez de saturar el upstream (evita "ResourceExhausted 33/32").</span>
        </label>
      </div>

      <div class="flex gap-3 pt-1">
        <button
          onclick={confirmAdd}
          disabled={!addForm.instanceId || !addForm.model}
          class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
        >{editingId !== null ? (saving ? '...' : 'Guardar') : 'Añadir'}</button>
        <button
          onclick={() => { addModal = false; editingId = null }}
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
        >Cancelar</button>
      </div>
    </div>
  </div>
{/if}

<!-- Modal: crear model_name -->
{#if newModelModal}
  <div class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
    <div class="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-sm p-6 space-y-4">
      <h2 class="text-base font-semibold">Nuevo model_name</h2>
      <p class="text-xs text-gray-500">
        Se accede vía <code class="text-violet-400 font-mono">infinity/{"<name>"}</code>.
        Si lo llamás <code>sonnet</code>, se invoca con <code>infinity/sonnet</code>.
      </p>
      <label class="block">
        <span class="text-xs text-gray-400 mb-1 block">Nombre</span>
        <input
          bind:value={newModelName}
          placeholder="sonnet, haiku, custom-model"
          onkeydown={(e) => { if (e.key === 'Enter') confirmCreateModel() }}
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono text-gray-100 focus:outline-none focus:border-violet-500"
        />
      </label>
      <div class="flex gap-3 pt-1">
        <button
          onclick={confirmCreateModel}
          disabled={!newModelName.trim()}
          class="flex-1 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
        >Crear y seleccionar</button>
        <button
          onclick={() => (newModelModal = false)}
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
        >Cancelar</button>
      </div>
    </div>
  </div>
{/if}
