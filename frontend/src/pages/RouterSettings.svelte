<script>
  import { api } from '../lib/api.js'

  let loading = $state(true)
  let saving = $state(false)
  let error = $state('')
  let saved = $state(false)

  let hangingThreshold = $state(30)
  let numRetries = $state(2)
  let incallRetryBaseDelay = $state(0.25)
  let maxParallelRequestsTimeout = $state(120)

  async function load() {
    loading = true
    try {
      const settings = await api.getRouterSettings()
      hangingThreshold = settings.hanging_threshold ?? 30
      numRetries = settings.num_retries ?? 2
      incallRetryBaseDelay = settings.incall_retry_base_delay ?? 0.25
      maxParallelRequestsTimeout = settings.max_parallel_requests_timeout ?? 120
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  $effect(() => { load() })

  async function save() {
    saving = true
    error = ''
    saved = false
    try {
      await api.bulkUpdateRouterSettings({
        hanging_threshold: Number(hangingThreshold),
        num_retries: Number(numRetries),
        incall_retry_base_delay: Number(incallRetryBaseDelay),
        max_parallel_requests_timeout: Number(maxParallelRequestsTimeout),
      })
      saved = true
      setTimeout(() => { saved = false }, 2500)
    } catch (e) {
      error = e.message
    } finally {
      saving = false
    }
  }
</script>

<div class="max-w-2xl space-y-6">
  <div>
    <h1 class="text-xl font-semibold text-gray-100">Router Settings</h1>
    <p class="text-sm text-gray-500 mt-1">
      Timeouts y reintentos globales del router. Los cambios se aplican a la siguiente petición
      (invalidan la caché del router al instante).
    </p>
  </div>

  {#if error}
    <div class="bg-red-900/40 border border-red-700 rounded-lg px-4 py-2 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="text-gray-500 text-sm">Cargando...</div>
  {:else}
    <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-5">
      <div>
        <label class="block text-sm font-medium text-gray-300 mb-1" for="hanging">
          Hanging threshold (segundos)
        </label>
        <p class="text-xs text-gray-500 mb-2">
          Tiempo máximo sin recibir bytes del upstream —tanto para el primer token como a mitad
          de un stream ya en marcha— antes de darlo por colgado, cortarlo y probar el siguiente
          deployment/fallback. Con 0 no hay límite (espera indefinida).
        </p>
        <input
          id="hanging"
          type="number"
          min="0"
          step="1"
          bind:value={hangingThreshold}
          class="w-40 bg-gray-950 border border-gray-800 rounded-md px-3 py-1.5 text-sm text-gray-200"
        />
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-300 mb-1" for="retries">
          Reintentos en-llamada (num_retries)
        </label>
        <p class="text-xs text-gray-500 mb-2">
          Reintentos con el mismo deployment antes de marcar cooldown y pasar al siguiente,
          para errores transitorios (5xx, rate limit breve, saturación).
        </p>
        <input
          id="retries"
          type="number"
          min="0"
          step="1"
          bind:value={numRetries}
          class="w-40 bg-gray-950 border border-gray-800 rounded-md px-3 py-1.5 text-sm text-gray-200"
        />
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-300 mb-1" for="backoff">
          Backoff base (segundos)
        </label>
        <p class="text-xs text-gray-500 mb-2">
          Base del backoff exponencial con jitter entre reintentos en-llamada.
        </p>
        <input
          id="backoff"
          type="number"
          min="0"
          step="0.05"
          bind:value={incallRetryBaseDelay}
          class="w-40 bg-gray-950 border border-gray-800 rounded-md px-3 py-1.5 text-sm text-gray-200"
        />
      </div>

      <div>
        <label class="block text-sm font-medium text-gray-300 mb-1" for="parallelwait">
          Espera máxima en cola (segundos)
        </label>
        <p class="text-xs text-gray-500 mb-2">
          Si un deployment tiene <code class="text-gray-400">max_parallel_requests</code> y está
          saturado, tiempo máximo que una petición espera en cola antes de pasar al siguiente
          deployment/fallback.
        </p>
        <input
          id="parallelwait"
          type="number"
          min="0"
          step="1"
          bind:value={maxParallelRequestsTimeout}
          class="w-40 bg-gray-950 border border-gray-800 rounded-md px-3 py-1.5 text-sm text-gray-200"
        />
      </div>

      <div class="flex items-center gap-3 pt-2">
        <button
          onclick={save}
          disabled={saving}
          class="px-4 py-2 rounded-md text-sm font-medium bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-50"
        >
          {saving ? 'Guardando...' : 'Guardar'}
        </button>
        {#if saved}
          <span class="text-sm text-green-400">Guardado.</span>
        {/if}
      </div>
    </div>
  {/if}
</div>
