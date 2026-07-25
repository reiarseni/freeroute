<script>
  import { api } from '../lib/api.js'

  let logs = $state([])
  let activeLogs = $state([])
  let loading = $state(true)
  let error = $state('')
  let autoRefresh = $state(true)
  let now = $state(Date.now())
  let interval
  let activeInterval
  let tickInterval

  async function load() {
    try {
      logs = await api.getLogs(100)
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  async function loadActive() {
    try {
      activeLogs = await api.getActiveLogs()
    } catch {
      // No bloquear la tabla principal si esto falla.
    }
  }

  $effect(() => {
    load()
    loadActive()
    tickInterval = setInterval(() => { now = Date.now() }, 1000)
    return () => clearInterval(tickInterval)
  })

  $effect(() => {
    clearInterval(interval)
    clearInterval(activeInterval)
    if (autoRefresh) {
      interval = setInterval(load, 5000)
      activeInterval = setInterval(loadActive, 1000)
    }
    return () => {
      clearInterval(interval)
      clearInterval(activeInterval)
    }
  })

  function elapsed(startedAt) {
    return Math.max(0, Math.round(now - startedAt * 1000))
  }

  function statusColor(code) {
    if (code === 0) return 'text-orange-400'
    if (!code) return 'text-gray-500'
    if (code < 300) return 'text-green-400'
    if (code < 500) return 'text-yellow-400'
    return 'text-red-400'
  }

  function chainColor(name) {
    if (!name) return 'text-gray-400'
    // Soportar tanto "haiku" (legacy) como "infinity/haiku" (nuevo)
    const n = name.includes('/') ? name.split('/').pop() : name
    return { haiku: 'text-green-400', sonnet: 'text-blue-400', opus: 'text-violet-400' }[n] ?? 'text-gray-300'
  }

  function fmt(ts) {
    if (!ts) return '—'
    return ts.replace('T', ' ').substring(0, 19)
  }

  function proxyPort(proxyType) {
    return proxyType === 'anthropic' ? '8788' : '8787'
  }
</script>

<div class="space-y-4">
  <div class="flex items-center justify-between">
    <h1 class="text-xl font-semibold text-gray-100">Request Logs</h1>
    <div class="flex items-center gap-3">
      <label class="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
        <input type="checkbox" bind:checked={autoRefresh} class="accent-violet-500" />
        Auto-refresh 5s
      </label>
      <button
        onclick={load}
        class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
      >
        Actualizar
      </button>
    </div>
  </div>

  {#if error}
    <div class="bg-red-900/40 border border-red-700 rounded-lg px-4 py-2 text-red-300 text-sm">{error}</div>
  {/if}

  <div class="bg-gray-900 border border-gray-800 rounded-2xl overflow-hidden">
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-gray-800 text-left text-xs text-gray-500 uppercase tracking-wider">
          <th class="px-4 py-3">Timestamp</th>
          <th class="px-4 py-3">Proxy</th>
          <th class="px-4 py-3">Model name</th>
          <th class="px-4 py-3">Original model</th>
          <th class="px-4 py-3">API usada</th>
          <th class="px-4 py-3">Modelo real</th>
          <th class="px-4 py-3">Status</th>
          <th class="px-4 py-3">Latencia</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-800">
        {#each activeLogs as active (active.id)}
          <tr class="bg-violet-950/30">
            <td class="px-4 py-2.5 text-gray-500 font-mono text-xs whitespace-nowrap">
              <span class="inline-block h-2 w-2 rounded-full bg-violet-400 animate-pulse mr-1.5"></span>
              ahora
            </td>
            <td class="px-4 py-2.5">
              <span class="px-1.5 py-0.5 rounded text-xs font-mono whitespace-nowrap
                {active.proxy_type === 'anthropic' ? 'bg-purple-900/40 text-purple-300' : 'bg-blue-900/40 text-blue-300'}"
                title={active.proxy_type ?? '—'}
              >
                :{proxyPort(active.proxy_type)}
              </span>
            </td>
            <td class="px-4 py-2.5 font-medium {chainColor(active.model_name)}">{active.model_name ?? '—'}</td>
            <td class="px-4 py-2.5 text-gray-400 font-mono text-xs truncate max-w-[140px]">{active.original_model ?? '—'}</td>
            <td class="px-4 py-2.5 text-gray-600 text-xs font-mono">—</td>
            <td class="px-4 py-2.5 text-gray-600 text-xs font-mono">—</td>
            <td class="px-4 py-2.5 font-mono font-semibold text-violet-300">
              <span class="flex items-center gap-1.5">
                <svg class="animate-spin h-3 w-3 text-violet-400" viewBox="0 0 24 24" fill="none">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                </svg>
                en proceso
              </span>
            </td>
            <td class="px-4 py-2.5 text-violet-300 text-xs font-mono">{elapsed(active.started_at)}ms</td>
          </tr>
        {/each}
        {#if loading}
          <tr><td colspan="8" class="px-4 py-8 text-center text-gray-500">Cargando...</td></tr>
        {:else if logs.length === 0 && activeLogs.length === 0}
          <tr><td colspan="8" class="px-4 py-8 text-center text-gray-600">Sin logs todavía</td></tr>
        {:else}
          {#each logs as log (log.id)}
            <tr class="hover:bg-gray-800/50 transition-colors">
              <td class="px-4 py-2.5 text-gray-500 font-mono text-xs whitespace-nowrap">{fmt(log.ts)}</td>
              <td class="px-4 py-2.5">
                <span class="px-1.5 py-0.5 rounded text-xs font-mono whitespace-nowrap
                  {log.proxy_type === 'anthropic' ? 'bg-purple-900/40 text-purple-300' : 'bg-blue-900/40 text-blue-300'}"
                  title={log.proxy_type ?? '—'}
                >
                  :{proxyPort(log.proxy_type)}
                </span>
              </td>
              <td class="px-4 py-2.5 font-medium {chainColor(log.model_name ?? log.chain_id)}">{log.model_name ?? log.chain_id ?? '—'}</td>
              <td class="px-4 py-2.5 text-gray-400 font-mono text-xs truncate max-w-[140px]">{log.original_model ?? '—'}</td>
              <td class="px-4 py-2.5 text-gray-400 text-xs font-mono">{log.api_instance_id ?? '—'}</td>
              <td class="px-4 py-2.5 text-gray-300 font-mono text-xs truncate max-w-[180px]">{log.model_id ?? '—'}</td>
              <td class="px-4 py-2.5 font-mono font-semibold {statusColor(log.status_code)}">
                {#if log.status_code === 0}
                  <span title="Intento fallido dentro de la cadena de fallback (sin llegar a responder al cliente)">
                    {log.error_type ?? 'FALLO'}
                  </span>
                {:else}
                  {log.status_code ?? '—'}
                {/if}
              </td>
              <td class="px-4 py-2.5 text-gray-400 text-xs font-mono">{log.latency_ms ? log.latency_ms + 'ms' : '—'}</td>
            </tr>
          {/each}
        {/if}
      </tbody>
    </table>
  </div>
</div>
