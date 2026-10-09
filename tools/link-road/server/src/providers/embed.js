import {
  COSMOS_EMBED1_URL,
  COSMOS_EMBED1_URL_SET,
  GPU_BEARER_TOKEN,
  NVIDIA_API_KEY,
  NVIDIA_EMBED_MODEL,
  NVIDIA_EMBED_URL,
  NVIDIA_NIM_URL,
  authHeaders,
} from '../config.js'
import { withTrace } from '../trace.js'
import { embedModel, nimFetch } from './nim.js'

const DIM = 256

export const SYNONYMS = {
  pedestrian: ['person', 'people', 'walker', 'crosswalk'],
  person: ['pedestrian', 'people', 'walker'],
  people: ['person', 'crowd', 'pedestrian'],
  crash: ['car', 'truck', 'collision', 'wreck', 'accident'],
  collision: ['crash', 'car', 'truck'],
  breakdown: ['car', 'truck', 'stalled', 'person'],
  stalled: ['car', 'truck', 'breakdown'],
  cyclist: ['bicycle', 'bike'],
  bicycle: ['cyclist', 'bike'],
  animal: ['dog', 'horse', 'deer', 'cow'],
  deer: ['animal', 'horse'],
  hat: ['person', 'helmet'],
  helmet: ['person', 'hat'],
  debris: ['object', 'suitcase', 'chair'],
  pothole: ['hole', 'asphalt', 'road', 'crack', 'pit'],
  hole: ['pothole', 'road'],
}

function tokens(text) {
  const words = String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
  const expanded = []
  for (const word of words) {
    expanded.push(word)
    for (const extra of SYNONYMS[word] || []) expanded.push(extra)
  }
  return expanded
}

function hashToken(token) {
  let h = 2166136261
  for (let i = 0; i < token.length; i += 1) h = Math.imul(h ^ token.charCodeAt(i), 16777619)
  return Math.abs(h) % DIM
}

export function lexicalEmbed(text) {
  const vector = new Array(DIM).fill(0)
  for (const token of tokens(text)) vector[hashToken(token)] += 1
  const norm = Math.sqrt(vector.reduce((sum, v) => sum + v * v, 0)) || 1
  return vector.map((v) => v / norm)
}

export function cosine(a = [], b = []) {
  const n = Math.min(a.length, b.length)
  let dot = 0
  let left = 0
  let right = 0
  for (let i = 0; i < n; i += 1) {
    dot += a[i] * b[i]
    left += a[i] * a[i]
    right += b[i] * b[i]
  }
  return dot / ((Math.sqrt(left) || 1) * (Math.sqrt(right) || 1))
}

async function embed1(text) {
  const model = await embedModel()
  const data = await nimFetch(COSMOS_EMBED1_URL, '/v1/embeddings', {
    method: 'POST',
    body: JSON.stringify({
      input: text,
      model,
      request_type: 'query',
      encoding_format: 'float',
    }),
    timeout: 20000,
  })
  const vector = data.data?.[0]?.embedding
  if (!vector?.length) throw new Error('empty embedding')
  return vector
}

async function cloudEmbed(text) {
  const url = `${NVIDIA_EMBED_URL || NVIDIA_NIM_URL}/embeddings`
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(NVIDIA_API_KEY) },
    body: JSON.stringify({ model: NVIDIA_EMBED_MODEL, input: [text], input_type: 'query' }),
  })
  if (!response.ok) throw new Error(`embed ${response.status}`)
  const data = await response.json()
  const vector = data.data?.[0]?.embedding
  if (!vector?.length) throw new Error('empty embedding')
  return vector
}

export function embedMode() {
  if (GPU_BEARER_TOKEN || COSMOS_EMBED1_URL_SET) return 'nim'
  if (NVIDIA_EMBED_URL || NVIDIA_API_KEY) return 'cloud'
  return 'off'
}

export async function embed(text) {
  return withTrace(
    'embed',
    async () => {
      const mode = embedMode()
      if (mode === 'nim') return embed1(text)
      if (mode === 'cloud') return cloudEmbed(text)
      throw new Error('COSMOS EMBED NOT CONFIGURED')
    },
    { mode: embedMode() },
  )
}
