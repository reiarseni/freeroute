<script>
  import Deployments from './pages/Deployments.svelte'
  import Keys from './pages/Keys.svelte'
  import Providers from './pages/Providers.svelte'
  import Logs from './pages/Logs.svelte'
  import Setup from './pages/Setup.svelte'
  import Chat from './pages/Chat.svelte'
  import RouterSettings from './pages/RouterSettings.svelte'

  const PAGES = ['deployments', 'keys', 'providers', 'logs', 'chat', 'settings', 'setup']

  function pathToPage(path) {
    const seg = path.replace(/^\//, '').split('/')[0] || 'deployments'
    return PAGES.includes(seg) ? seg : 'deployments'
  }

  let page = $state(pathToPage(window.location.pathname))

  function navigate(id) {
    page = id
    history.pushState({}, '', '/' + id)
  }

  $effect(() => {
    function onPop() { page = pathToPage(window.location.pathname) }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  })
</script>

<div class="min-h-screen bg-gray-950 text-gray-100 flex flex-col">
  <!-- Header -->
  <header class="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center gap-6">
    <div class="flex items-center gap-2">
      <span class="text-violet-400 font-bold text-lg tracking-tight">∞ FreeRoute</span>
      <span class="text-gray-500 text-sm">v4</span>
    </div>
    <nav class="flex gap-1 ml-4">
      {#each [['deployments', 'Deployments'], ['keys', 'API Keys'], ['providers', 'Providers'], ['logs', 'Logs'], ['chat', 'Chat'], ['settings', 'Settings'], ['setup', 'Setup']] as [id, label]}
        <button
          onclick={() => navigate(id)}
          class="px-4 py-1.5 rounded-md text-sm font-medium transition-colors
            {page === id
              ? 'bg-violet-600 text-white'
              : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
        >
          {label}
        </button>
      {/each}
    </nav>
    <div class="ml-auto flex gap-3 text-xs text-gray-500">
      <span>OpenAI proxy <code class="text-gray-400">:8787</code></span>
      <span>Anthropic proxy <code class="text-gray-400">:8788</code></span>
    </div>
  </header>

  <!-- Content -->
  <main class="flex-1 p-6">
    {#if page === 'deployments'}
      <Deployments />
    {:else if page === 'keys'}
      <Keys />
    {:else if page === 'providers'}
      <Providers />
    {:else if page === 'logs'}
      <Logs />
    {:else if page === 'chat'}
      <Chat />
    {:else if page === 'settings'}
      <RouterSettings />
    {:else}
      <Setup />
    {/if}
  </main>
</div>
