const BASE = ''

async function req(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Error desconocido')
  }
  return res.json()
}

export const api = {
  // API Instances
  getInstances: () => req('GET', '/api/instances'),
  createInstance: (data) => req('POST', '/api/instances', data),
  updateInstance: (id, data) => req('PUT', `/api/instances/${id}`, data),
  deleteInstance: (id) => req('DELETE', `/api/instances/${id}`),

  // Providers (CRUD data-driven)
  getProviders: () => req('GET', '/api/providers'),
  createProvider: (data) => req('POST', '/api/providers', data),
  updateProvider: (name, data) => req('PUT', `/api/providers/${encodeURIComponent(name)}`, data),
  deleteProvider: (name) => req('DELETE', `/api/providers/${encodeURIComponent(name)}`),

  // Deployments (reemplaza /api/chains)
  getDeployments: (modelName) =>
    req('GET', `/api/deployments${modelName ? `?model_name=${encodeURIComponent(modelName)}` : ''}`),
  getModelNames: () => req('GET', '/api/deployments/model-names'),
  createDeployment: (data) => req('POST', '/api/deployments', data),
  updateDeployment: (id, data) => req('PUT', `/api/deployments/${id}`, data),
  deleteDeployment: (id) => req('DELETE', `/api/deployments/${id}`),

  // Router settings (fallbacks, strategy, cooldown, limits)
  getRouterSettings: () => req('GET', '/api/router-settings'),
  updateRouterSetting: (key, value) => req('PUT', `/api/router-settings/${key}`, { key, value }),
  bulkUpdateRouterSettings: (settings) => req('PUT', '/api/router-settings', { settings }),
  deleteRouterSetting: (key) => req('DELETE', `/api/router-settings/${key}`),

  // Router stats (latency, cooldown, in-flight)
  getLatencyStats: () => req('GET', '/api/router-stats/latency'),
  getCooldownStats: () => req('GET', '/api/router-stats/cooldowns'),
  getInFlight: () => req('GET', '/api/router-stats/in-flight'),

  // Provider models — filter: "auto" | "free" | "paid" | "all"
  getProviderModels: (instanceId, filter = 'auto') =>
    req('GET', `/api/provider-models?instance_id=${instanceId}&filter=${filter}`),

  // Logs
  getLogs: (limit = 100) => req('GET', `/api/logs?limit=${limit}`),
  getActiveLogs: () => req('GET', '/api/logs/active'),

  // OAuth device flow (providers auth_type=oauth_device)
  startOauthFlow: (provider, name = '', profileArn = '') =>
    req('POST', `/api/oauth/${encodeURIComponent(provider)}/start`, { name, profile_arn: profileArn }),
  pollOauthStatus: (provider, flowId) =>
    req('GET', `/api/oauth/${encodeURIComponent(provider)}/status/${flowId}`),
  reauthInstance: (provider, instanceId, profileArn = '') =>
    req('POST', `/api/oauth/${encodeURIComponent(provider)}/reauth/${instanceId}`, { profile_arn: profileArn }),
}
