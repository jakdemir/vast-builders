import { CANARY_1B_URL, COSMOS3_REASON_URL, COSMOS_EMBED1_URL, YOLO_URL, gpuHeaders } from '../config.js'
import { canaryMode } from './canary.js'
import { cosmosMode } from './cosmos.js'
import { embedMode } from './embed.js'
import { complete, llmMode } from './llm.js'
import { vssConfigured, vssToken } from './vss.js'

const TTL_MS = 30 * 1000
const LLM_TTL_MS = 5 * 60 * 1000
let cached = { at: 0, value: null }
let llmCached = { at: 0, ok: false, error: '' }

async function ready(url) {
  try {
    const response = await fetch(url, { headers: gpuHeaders(), signal: AbortSignal.timeout(4000) })
    return response.ok ? { ok: true } : { ok: false, error: `HTTP ${response.status}` }
  } catch (error) {
    return { ok: false, error: error.message }
  }
}

async function search() {
  if (!vssConfigured()) return { ok: false, error: 'VSS credentials missing', off: true }
  try {
    await vssToken()
    return { ok: true }
  } catch (error) {
    return { ok: false, error: error.message }
  }
}

async function llm() {
  if (llmMode() !== 'live') return { ok: false, error: 'WANDB_API_KEY missing', off: true }
  if (Date.now() - llmCached.at < LLM_TTL_MS) return llmCached
  try {
    await complete({ system: 'Reply with OK.', user: 'ping' })
    llmCached = { at: Date.now(), ok: true, error: '' }
  } catch (error) {
    llmCached = { at: Date.now(), ok: false, error: error.message }
  }
  return llmCached
}

const mode = (check) => (check.off ? 'off' : check.ok ? 'live' : 'down')

export async function verifyStack(force = false) {
  if (!force && cached.value && Date.now() - cached.at < TTL_MS) return cached.value
  const off = { ok: false, off: true }
  const [cosmos, embed, yolo, canary, vss, wandb] = await Promise.all([
    cosmosMode() === 'off' ? off : ready(`${COSMOS3_REASON_URL}/v1/health/ready`),
    embedMode() === 'off' ? off : ready(`${COSMOS_EMBED1_URL}/v1/health/ready`),
    ready(`${YOLO_URL}/healthz`),
    canaryMode() === 'off' ? off : ready(`${CANARY_1B_URL}/v1/health/ready`),
    search(),
    llm(),
  ])
  const checks = { cosmos, embed, yolo, canary, search: vss, llm: wandb }
  const value = {
    modes: Object.fromEntries(Object.entries(checks).map(([key, check]) => [key, mode(check)])),
    errors: Object.fromEntries(
      Object.entries(checks)
        .filter(([, check]) => !check.ok && check.error)
        .map(([key, check]) => [key, String(check.error).slice(0, 120)]),
    ),
  }
  cached = { at: Date.now(), value }
  return value
}
