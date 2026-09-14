// Thin wrapper over the FastAPI backend. Every call goes through the Vite proxy
// at /api, so there is no base URL to configure.

async function get(path, params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null),
  )
  const url = `/api${path}${qs.toString() ? `?${qs}` : ''}`
  const res = await fetch(url)
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status} ${path}: ${body.slice(0, 200)}`)
  }
  return res.json()
}

export const fetchMeta = () => get('/meta')
export const fetchPoints = (limit) => get('/points', { limit })
export const runQuery = (params) => get('/query', params)
export const runKnn = (params) => get('/knn', params)
export const fetchCells = (params) => get('/cells', params)
export const runCompare = (params) => get('/compare', params)
