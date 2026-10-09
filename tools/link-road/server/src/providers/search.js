import { VAST_API_KEY, VAST_SEARCH_URL, VAST_UPLOAD, authHeaders } from '../config.js'
import { allSegments, getClip, publicClip } from '../store.js'
import { withTrace } from '../trace.js'
import { cosine, embed } from './embed.js'
import { vssConfigured, vssExplore, vssSearch, vssUpload } from './vss.js'

function hitFromSegment(segment, score, clip) {
  return {
    id: segment.id,
    clipId: segment.clipId,
    start: segment.start,
    end: segment.end,
    caption: segment.caption,
    score: Number(score.toFixed(3)),
    thumbnail: segment.thumbnail || null,
    name: clip?.name || segment.name || 'CLIP',
    url: clip ? publicClip(clip).url : null,
    provider: 'local',
  }
}

async function legacyVastQuery(text) {
  const response = await fetch(`${VAST_SEARCH_URL}/api/v1/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(VAST_API_KEY) },
    body: JSON.stringify({ query: text, top_k: 8 }),
  })
  if (!response.ok) throw new Error(`VAST search ${response.status}`)
  const data = await response.json()
  const rows = data.results || data.hits || data.data || []
  return rows.map((row, index) => ({
    id: row.id || `vast-${index}`,
    clipId: row.clipId || row.video_name || row.video_id || null,
    start: Number(row.start_time ?? row.start ?? 0),
    end: Number(row.end_time ?? row.end ?? 0),
    caption: row.description || row.caption || row.text || '',
    score: Number(row.similarity ?? row.score ?? 0),
    thumbnail: row.screenshot_url || row.thumbnail || null,
    name: row.video_name || row.name || 'VAST',
    url: row.url || null,
    provider: 'vast',
  }))
}

export async function ingest(clip, file) {
  return withTrace(
    'search.ingest',
    async () => {
      if (VAST_UPLOAD && vssConfigured() && file?.buffer) {
        try {
          const uploaded = await vssUpload(file, { tags: 'link-road', capture_type: 'traffic' })
          return { provider: 'vast', objectKey: uploaded.object_key || null }
        } catch {
          return { provider: 'local' }
        }
      }
      return { provider: 'local' }
    },
    { clipId: clip.id },
  )
}

const ISSUE_KINDS = [
  {
    id: 'pothole',
    re: /\bpot\s*holes?\b/,
    label: 'POTHOLE',
    queries: ['cityRoad_potHoles', 'road surface damage cracks holes pavement', 'pothole in the roadway'],
    keep: /pothole|potholes|pot.?holes|cracked|uneven|pavement (damage|crack)|hole in the (road|asphalt)|broken (asphalt|pavement)/i,
    reject: /\bdonut\b/i,
  },
  {
    id: 'debris',
    re: /\b(debris|tires?|tyres?|junk|wreck(age)?)\b/,
    label: 'DEBRIS',
    queries: ['debris on the roadway', 'junk or wreckage sitting in the travel lane', 'rubble or trash blocking the road'],
    keep: /\b(debris|junk|wreck(age)?|rubble|litter)\b|\btrash(?! (bin|can|bag))\b|\b(flat )?tyres?\b|\btires?\b/i,
    reject: /\b(pedestrian|crosswalk|crossing|people|person|crowd|woman|women|\bman\b|pothole|pot.?holes|donut)\b/i,
  },
]

export function planSearch(text) {
  const raw = String(text || '')
  const listing = /\b(show|list|find|search|all|open|unresolved|outstanding|issues?|tickets?)\b/i.test(raw)
  const kind = ISSUE_KINDS.find((item) => item.re.test(raw))
  const unresolved = /\b(unresolved|open|outstanding)\b/i.test(raw)
  if (kind && (listing || unresolved)) {
    return {
      queries: kind.queries,
      keep: kind.keep,
      reject: kind.reject,
      issue: kind.id,
      label: kind.label,
      unresolved: unresolved || listing,
      minSimilarity: 0.16,
      limit: 16,
    }
  }
  if (kind) {
    return {
      queries: kind.queries,
      keep: kind.keep,
      reject: kind.reject,
      issue: kind.id,
      label: kind.label,
      unresolved: false,
      minSimilarity: 0.18,
      limit: 12,
    }
  }
  return {
    queries: [raw],
    keep: null,
    reject: null,
    issue: null,
    label: '',
    unresolved: false,
    minSimilarity: 0.3,
    limit: 8,
  }
}

function issueBlob(hit) {
  return `${hit.name || ''} ${hit.filename || ''} ${hit.source || ''} ${hit.caption || ''} ${hit.clipId || ''}`
}

function rankIssue(hit, plan) {
  const blob = issueBlob(hit)
  let score = Number(hit.score || 0)
  if (plan.issue === 'pothole' && /pothole|potholes|pot.?holes/i.test(blob)) score += 0.28
  if (plan.issue === 'debris' && /debris|tire|tyre|junk|wreck/i.test(blob)) score += 0.22
  return { ...hit, score: Math.min(1, score), issue: plan.issue || hit.issue || null, kind: plan.issue || hit.kind || null }
}

export async function query(text, limit = 8) {
  const plan = planSearch(text)
  const cap = Math.max(limit, plan.limit)
  return withTrace(
    'search.query',
    async () => {
      const remote = []
      let synthesis = ''
      if (vssConfigured()) {
        const archives = await Promise.all(plan.queries.map((q) => vssSearch(q, cap, plan.minSimilarity)))
        for (const archive of archives) {
          remote.push(...(archive.hits || []).map((hit) => rankIssue(hit, plan)))
          if (!synthesis) synthesis = archive.synthesis || ''
        }
      } else if (VAST_SEARCH_URL && VAST_API_KEY) {
        remote.push(...(await legacyVastQuery(text)))
      }
      const segments = await allSegments()
      const vector = segments.length ? await embed(text) : []
      const local = []
      for (const segment of segments) {
        const score = cosine(vector, segment.vector || [])
        if (score < 0.05) continue
        const clip = await getClip(segment.clipId)
        local.push(hitFromSegment(segment, score, clip))
      }
      local.sort((a, b) => b.score - a.score)
      const seen = new Set()
      const merged = []
      const ranked = [...remote, ...local].sort((a, b) => (b.score || 0) - (a.score || 0))
      for (const hit of ranked) {
        const key = `${hit.source || hit.clipId || hit.id}:${hit.start}`
        if (seen.has(key)) continue
        const blob = issueBlob(hit)
        if (plan.reject && plan.reject.test(blob) && !(plan.keep && plan.keep.test(blob))) continue
        if (plan.keep && !plan.keep.test(blob)) continue
        seen.add(key)
        merged.push(hit)
        if (merged.length >= cap) break
      }
      return {
        hits: merged,
        synthesis: plan.issue ? '' : synthesis,
        issue: plan.issue,
        label: plan.label,
        unresolved: plan.unresolved,
      }
    },
    { q: String(text || '').slice(0, 80), issue: plan.issue || '' },
  )
}

const ARCHIVE_KINDS = ISSUE_KINDS.map((kind) => ({
  ...kind,
  want: kind.id === 'pothole' ? 2 : 1,
  exploreMax: kind.id === 'pothole' ? 300 : 200,
}))

async function fetchKind(kind, want, seen) {
  const hits = []
  const archives = await Promise.all(kind.queries.map((q) => vssSearch(q, 8, 0.16).catch(() => ({ hits: [] }))))
  const ranked = archives
    .flatMap((archive, index) => (archive.hits || []).map((hit) => ({ ...hit, query: kind.queries[index] })))
    .sort((a, b) => (b.score || 0) - (a.score || 0))
  const take = (hit) => {
    const key = hit.source || `${hit.clipId}:${hit.start}`
    const parent = hit.clipId || hit.name || key
    const blob = issueBlob(hit)
    if (!key || seen.has(key) || seen.has(parent)) return false
    if (kind.reject && kind.reject.test(blob)) return false
    if (kind.keep && !kind.keep.test(blob)) return false
    seen.add(key)
    seen.add(parent)
    hits.push({
      ...hit,
      issue: kind.id,
      kind: kind.id,
      label: kind.label,
      query: hit.query || kind.id,
    })
    return hits.length >= want
  }
  for (const hit of ranked) {
    if (take(hit)) return hits
  }
  for (let offset = 0; offset < (kind.exploreMax || 300) && hits.length < want; offset += 100) {
    const explored = await vssExplore(100, offset).catch(() => [])
    for (const hit of explored) {
      if (take({ ...hit, score: hit.score || 0.4, query: `${kind.id} explore` })) return hits
    }
    if (!explored.length) break
  }
  return hits
}

export async function randomObstructions(limit = 4) {
  return withTrace(
    'search.obstructions',
    async () => {
      if (!vssConfigured()) throw new Error('VSS CREDENTIALS MISSING')
      const seen = new Set()
      const groups = []
      for (const kind of ARCHIVE_KINDS) groups.push(await fetchKind(kind, kind.want, seen))
      const hits = groups.flat().slice(0, limit)
      return { hits, query: 'pothole debris archive' }
    },
    { limit },
  )
}
