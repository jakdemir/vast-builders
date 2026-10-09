import {
  COSMOS3_REASON_MODEL,
  COSMOS3_REASON_URL,
  COSMOS3_REASON_URL_SET,
  COSMOS_EMBED1_MODEL,
  COSMOS_EMBED1_URL,
  COSMOS_EMBED1_URL_SET,
  GPU_BEARER_TOKEN,
  YOLO_URL,
  gpuHeaders,
} from '../config.js'

const modelCache = new Map()

function join(base, path) {
  return `${String(base || '').replace(/\/$/, '')}${path}`
}

export async function nimFetch(base, path, init = {}) {
  const json = init.body != null && !(init.headers && init.headers['Content-Type'])
  const response = await fetch(join(base, path), {
    ...init,
    headers: { ...gpuHeaders(json), ...(init.headers || {}) },
    signal: init.signal || AbortSignal.timeout(init.timeout || 45000),
  })
  if (!response.ok) {
    await response.text().catch(() => '')
    throw new Error(`NIM ${path} ${response.status}`)
  }
  const type = response.headers.get('content-type') || ''
  if (type.includes('application/json')) return response.json()
  return response.text()
}

export async function firstModel(base, fallback) {
  const key = String(base || '')
  if (modelCache.has(key)) return modelCache.get(key)
  const data = await nimFetch(base, '/v1/models', { method: 'GET', timeout: 8000 })
  const id = data?.data?.[0]?.id || fallback
  modelCache.set(key, id)
  return id
}

export async function cosmosModel() {
  try {
    return await firstModel(COSMOS3_REASON_URL, COSMOS3_REASON_MODEL)
  } catch {
    return COSMOS3_REASON_MODEL
  }
}

export async function embedModel() {
  try {
    return await firstModel(COSMOS_EMBED1_URL, COSMOS_EMBED1_MODEL)
  } catch {
    return COSMOS_EMBED1_MODEL
  }
}

async function probe(url) {
  try {
    const response = await fetch(url, {
      headers: gpuHeaders(),
      signal: AbortSignal.timeout(2500),
    })
    return response.status
  } catch {
    return 0
  }
}

export async function probeGpu() {
  const result = {
    token: Boolean(GPU_BEARER_TOKEN),
    cosmos: 0,
    embed: 0,
    yolo: 0,
  }
  if (!GPU_BEARER_TOKEN && !COSMOS3_REASON_URL_SET && !COSMOS_EMBED1_URL_SET) return result
  const [cosmos, embed, yolo] = await Promise.all([
    probe(join(COSMOS3_REASON_URL, '/v1/health/ready')),
    probe(join(COSMOS_EMBED1_URL, '/v1/health/ready')),
    probe(join(YOLO_URL, '/healthz')),
  ])
  result.cosmos = cosmos
  result.embed = embed
  result.yolo = yolo
  return result
}
