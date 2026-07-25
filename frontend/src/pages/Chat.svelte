<script>
  import { api } from '../lib/api.js'

  let modelNames = $state([])
  let loadingModels = $state(true)
  let selectedModel = $state('')
  let messages = $state([])
  let input = $state('')
  let sending = $state(false)

  $effect(() => {
    api.getModelNames()
      .then((rows) => {
        modelNames = rows.map((r) => (typeof r === 'string' ? r : r.model_name))
        if (modelNames.length > 0) selectedModel = modelNames[0]
      })
      .catch(() => { modelNames = [] })
      .finally(() => { loadingModels = false })
  })

  async function streamChat(history, model, onToken, onMeta) {
    const startedAt = performance.now()
    let firstTokenAt = null

    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages: history, stream: true }),
    })

    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      const detail = body.detail
      const message =
        typeof detail === 'string' ? detail
        : detail?.message ? detail.message
        : body.error || res.statusText
      throw new Error(message)
    }

    const provider = res.headers.get('X-FreeRoute-Provider')
    const modelId = res.headers.get('X-FreeRoute-Model')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let lastModel = null
    let sawDone = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const data = trimmed.slice(5).trim()
        if (data === '[DONE]') { sawDone = true; continue }
        if (!data) continue
        let chunk
        try { chunk = JSON.parse(data) } catch { continue }
        if (chunk.model) lastModel = chunk.model
        const delta = chunk.choices?.[0]?.delta?.content
        if (delta) {
          if (firstTokenAt === null) firstTokenAt = performance.now()
          onToken(delta)
        }
      }
    }

    if (!sawDone) {
      throw new Error('__STREAM_INTERRUPTED__')
    }

    onMeta({
      provider: provider || null,
      model: modelId || lastModel || null,
      latencyMs: firstTokenAt !== null ? Math.round(firstTokenAt - startedAt) : null,
    })
  }

  async function send() {
    const text = input.trim()
    if (!text || sending || !selectedModel) return

    const history = messages
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({ role: m.role, content: m.content }))
    history.push({ role: 'user', content: text })

    messages.push({ role: 'user', content: text })
    input = ''
    sending = true

    messages.push({ role: 'assistant', content: '', meta: null })
    const assistantMsg = messages[messages.length - 1]

    try {
      await streamChat(
        history,
        selectedModel,
        (delta) => { assistantMsg.content += delta },
        (meta) => { assistantMsg.meta = meta },
      )
    } catch (err) {
      if (err.message === '__STREAM_INTERRUPTED__') {
        messages.push({ role: 'error', content: 'El stream se interrumpió antes de completarse.' })
      } else {
        messages.push({ role: 'error', content: err.message || 'Error desconocido' })
      }
    } finally {
      sending = false
    }
  }

  function onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }
</script>

<div class="flex flex-col h-[calc(100vh-8rem)] max-w-3xl mx-auto">
  <div class="flex items-center gap-3 mb-4">
    <span class="text-sm text-gray-400">Deployment</span>
    {#if loadingModels}
      <span class="text-sm text-gray-500">Cargando...</span>
    {:else if modelNames.length === 0}
      <span class="text-sm text-amber-400">
        No hay deployments configurados, ve a la pestaña Deployments.
      </span>
    {:else}
      <select
        bind:value={selectedModel}
        class="bg-gray-900 border border-gray-800 rounded-md px-3 py-1.5 text-sm text-gray-200"
      >
        {#each modelNames as name}
          <option value={name}>{name}</option>
        {/each}
      </select>
    {/if}
  </div>

  <div class="flex-1 overflow-y-auto flex flex-col gap-3 pr-1">
    {#each messages as msg}
      {#if msg.role === 'user'}
        <div class="self-end max-w-[80%] bg-violet-600 text-white rounded-lg px-4 py-2 text-sm whitespace-pre-wrap">
          {msg.content}
        </div>
      {:else if msg.role === 'assistant'}
        <div class="self-start max-w-[80%]">
          <div class="bg-gray-900 border border-gray-800 rounded-lg px-4 py-2 text-sm whitespace-pre-wrap text-gray-100">
            {msg.content}
          </div>
          {#if msg.meta}
            <div class="text-xs text-gray-500 mt-1 px-1">
              {msg.meta.provider || '?'} · {msg.meta.model || '?'}
              {#if msg.meta.latencyMs !== null} · {msg.meta.latencyMs}ms hasta primer token{/if}
            </div>
          {/if}
        </div>
      {:else}
        <div class="self-start max-w-[80%] bg-red-950 border border-red-900 text-red-300 rounded-lg px-4 py-2 text-sm whitespace-pre-wrap">
          {msg.content}
        </div>
      {/if}
    {/each}
  </div>

  <div class="mt-4 flex gap-2">
    <textarea
      bind:value={input}
      onkeydown={onKeydown}
      disabled={sending || modelNames.length === 0}
      rows="2"
      placeholder="Escribe un mensaje..."
      class="flex-1 bg-gray-900 border border-gray-800 rounded-md px-3 py-2 text-sm text-gray-200 resize-none disabled:opacity-50"
    ></textarea>
    <button
      onclick={send}
      disabled={sending || !input.trim() || modelNames.length === 0}
      class="px-4 py-2 rounded-md text-sm font-medium bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      Enviar
    </button>
  </div>
</div>
