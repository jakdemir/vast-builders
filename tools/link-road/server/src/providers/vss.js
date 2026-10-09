import { INGRESS_URL, VSS_PASSWORD, VSS_USERNAME, publicPath } from '../config.js'

let cached = { token: '', at: 0 }
const TTL_MS = 50 * 60 * 1000

function backend() {
  return INGRESS_URL
}

export function vssConfigured() {
  return Boolean(INGRESS_URL && VSS_USERNAME && VSS_PASSWORD)
}

export function vssReachable() {
  return Boolean(INGRESS_URL)
}

async function postLogin() {
  const response = await fetch(`${backend()}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: VSS_USERNAME, password: VSS_PASSWORD }),
    signal: AbortSignal.timeout(12000),
  })
  if (!response.ok) throw new Error(`VSS login ${response.status}`)
  const data = await response.json()
  const token = data.access_token
  if (!token) throw new Error('VSS login missing token')
  cached = { token, at: Date.now() }
  return token
}

export async function vssToken(force = false) {
  if (!vssConfigured()) throw new Error('VSS credentials missing')
  if (!force && cached.token && Date.now() - cached.at < TTL_MS) return cached.token
  return postLogin()
}

async function vssFetch(path, init = {}, retry = true) {
  const token = await vssToken()
  const { timeout = 30000, signal, headers, ...rest } = init
  const response = await fetch(`${backend()}${path}`, {
    ...rest,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(rest.body && !(rest.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
      ...(headers || {}),
    },
    signal: signal || AbortSignal.timeout(timeout),
  })
  if (response.status === 401 && retry) {
    cached = { token: '', at: 0 }
    return vssFetch(path, init, false)
  }
  return response
}

function fileName(uri) {
  const text = String(uri || '')
  const last = text.split('/').pop() || text
  return decodeURIComponent(last).replace(/\.[a-z0-9]+$/i, '') || 'ARCHIVE'
}

function mapHit(row, index) {
  const source = row.preview_source || row.source || row.original_video || ''
  const start = Number(row.best_match_start_sec ?? row.start_sec ?? row.start_time ?? row.start ?? 0)
  const end = Number(row.best_match_end_sec ?? row.end_sec ?? row.end_time ?? row.end ?? start)
  return {
    id: source || row.original_video || `vast-${index}`,
    clipId: row.original_video || source || null,
    start,
    end,
    caption: row.reasoning_content || row.caption || row.description || row.text || '',
    score: Number(row.similarity_score ?? row.best_similarity ?? row.similarity ?? row.score ?? 0),
    thumbnail: row.thumbnail || row.screenshot_url || null,
    name: fileName(row.filename || row.original_video || source),
    filename: row.filename || '',
    source,
    url: source ? publicPath(`/api/archive/stream?source=${encodeURIComponent(source)}`) : null,
    provider: 'vast',
    location: row.location || '',
    camera_id: row.camera_id || '',
    capture_type: row.capture_type || '',
  }
}

export function parseArchiveQuery(text) {
  let query = String(text || '').trim()
  let time_filter = 'all'
  const rules = [
    [/\b(last|past) 5 ?m(in(utes)?)?\b/i, '5m'],
    [/\b(last|past) 15 ?m(in(utes)?)?\b/i, '15m'],
    [/\b(last|past) (an? )?hours?\b|\blast hour\b|\b1h\b/i, '1h'],
    [/\b(last|past) (24 ?h|day)\b/i, '24h'],
    [/\b(last|past) (week|7 ?d)\b/i, '7d'],
  ]
  for (const [re, value] of rules) {
    if (re.test(query)) {
      time_filter = value
      query = query.replace(re, ' ').replace(/\s+/g, ' ').trim()
      break
    }
  }
  return { query: query || String(text || '').trim(), time_filter }
}

export async function vssSearch(text, limit = 8, minSimilarity = 0.3) {
  const { query, time_filter } = parseArchiveQuery(text)
  let lastError = new Error('VSS search failed')
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await vssFetch('/api/v1/search', {
      method: 'POST',
      body: JSON.stringify({
        query,
        top_k: Math.min(100, Math.max(1, limit)),
        llm_top_n: 1,
        min_similarity: minSimilarity,
        time_filter,
        include_public: true,
      }),
      timeout: 40000,
    })
    if (response.status >= 500 && attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, 900))
      lastError = new Error(`VSS search ${response.status}`)
      continue
    }
    if (!response.ok) throw new Error(`VSS search ${response.status}`)
    const data = await response.json()
    const chunks = data.chunk_results || []
    const rows = data.results || data.hits || []
    const hits = (chunks.length ? chunks : rows).map(mapHit)
    return {
      hits,
      synthesis: data.llm_synthesis?.response || '',
      provider: 'vast',
    }
  }
  throw lastError
}

export async function vssExplore(limit = 48, offset = 0) {
  const response = await vssFetch(
    `/api/v1/videos/explore?scope=all&limit=${Math.min(100, Math.max(1, limit))}&offset=${Math.max(0, offset)}`,
    {
      timeout: 20000,
    },
  )
  if (!response.ok) throw new Error(`VSS explore ${response.status}`)
  const data = await response.json()
  return (data.chunks || []).map(mapHit)
}

export async function vssAsk(question, originalVideo) {
  const response = await vssFetch('/api/v1/agent/ask', {
    method: 'POST',
    body: JSON.stringify({
      question,
      original_video: originalVideo || null,
      top_k: 10,
    }),
    timeout: 60000,
  })
  if (!response.ok) {
    // Unscoped /agent/ask can 500 on the shared backend; search-and-answer covers the same case.
    if (!originalVideo) return vssSearchAndAnswer(question)
    throw new Error(`VSS ask ${response.status}`)
  }
  const data = await response.json()
  return {
    answer: data.answer || data.llm_synthesis?.response || '',
    evidence: data.evidence || null,
    provider: 'vss',
  }
}

export async function vssSearchAndAnswer(text, limit = 10) {
  const { query, time_filter } = parseArchiveQuery(text)
  const response = await vssFetch('/api/v1/agent/search-and-answer', {
    method: 'POST',
    body: JSON.stringify({
      query,
      top_k: Math.min(50, Math.max(1, limit)),
      min_similarity: 0.3,
      time_filter,
      include_public: true,
    }),
    timeout: 60000,
  })
  if (!response.ok) throw new Error(`VSS search-and-answer ${response.status}`)
  const data = await response.json()
  return {
    answer: data.answer || data.evidence?.llm_synthesis?.response || '',
    evidence: data.evidence || null,
    provider: 'vss',
  }
}

export async function vssStreamTarget(source) {
  const token = await vssToken()
  const encoded = encodeURIComponent(source)
  try {
    const response = await fetch(
      `${backend()}/api/v1/videos/playback-url?source=${encoded}&token=${encodeURIComponent(token)}&expires_in=3600`,
      { signal: AbortSignal.timeout(10000) },
    )
    if (response.ok) {
      const data = await response.json().catch(() => ({}))
      const url = data.url || data.playback_url || data.href
      if (url) return { url, token: null }
    }
  } catch {
    /* fall through to range proxy */
  }
  return {
    url: `${backend()}/api/v1/videos/stream?source=${encoded}&token=${encodeURIComponent(token)}`,
    token,
  }
}

export async function vssUpload(file, extras = {}) {
  const body = new FormData()
  body.set('file', new Blob([file.buffer], { type: file.type || 'video/mp4' }), file.name || 'clip.mp4')
  body.set('is_public', extras.is_public == null ? 'true' : String(extras.is_public))
  if (extras.tags) body.set('tags', extras.tags)
  if (extras.scenario) body.set('scenario', extras.scenario)
  if (extras.location) body.set('location', extras.location)
  if (extras.camera_id) body.set('camera_id', extras.camera_id)
  if (extras.capture_type) body.set('capture_type', extras.capture_type)
  const response = await vssFetch('/api/v1/videos/upload', { method: 'POST', body, timeout: 120000 })
  if (!response.ok) throw new Error(`VSS upload ${response.status}`)
  return response.json().catch(() => ({}))
}
