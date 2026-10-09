import { config as loadEnv } from 'dotenv'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const ROOT = path.resolve(here, '../..')
export const SERVER_ROOT = path.resolve(here, '..')

const trim = (value) => {
  const text = (value ?? '').trim()
  return text || ''
}

function applyEnvFile(filePath) {
  let text = ''
  try {
    text = fs.readFileSync(filePath, 'utf8')
  } catch {
    return false
  }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq < 1) continue
    const key = line.slice(0, eq).trim()
    let value = line.slice(eq + 1).trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    if (!key || !value) continue
    if (trim(process.env[key])) continue
    process.env[key] = value
  }
  return true
}

function loadTeamConfig() {
  const dotted = '/config/.config'
  if (fs.existsSync(dotted) && applyEnvFile(dotted)) return dotted
  let names = []
  try {
    names = fs.readdirSync('/config').filter((name) => name.endsWith('.config')).sort()
  } catch {
    return ''
  }
  if (names.length !== 1) return ''
  const filePath = path.join('/config', names[0])
  return applyEnvFile(filePath) ? filePath : ''
}

export const TEAM_CONFIG = loadTeamConfig()
loadEnv({ path: path.join(ROOT, '.env'), override: false })
if (process.env.WANDB_API_KEY) process.env.WANDB_API_KEY = process.env.WANDB_API_KEY.trim()

export const PORT = Number(process.env.PORT || 8787)
export const HOST = trim(process.env.HOST) || '0.0.0.0'
export const APP_BASE = trim(process.env.APP_BASE).replace(/\/$/, '')
export function publicPath(path) {
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${APP_BASE}${suffix}`
}
export const DATA_DIR = path.join(SERVER_ROOT, 'data')
export const CLIPS_DIR = path.join(DATA_DIR, 'clips')
export const STORE_PATH = path.join(DATA_DIR, 'store.json')

export const WANDB_API_KEY = trim(process.env.WANDB_API_KEY)
const WANDB_TEAM = trim(process.env.WANDB_TEAM)
const RAW_WANDB_PROJECT = trim(process.env.WANDB_PROJECT)
// W&B inference expects OpenAI-Project as "<team>/<project>"; the VM sets them separately.
export const WANDB_PROJECT =
  RAW_WANDB_PROJECT && WANDB_TEAM && !RAW_WANDB_PROJECT.includes('/')
    ? `${WANDB_TEAM}/${RAW_WANDB_PROJECT}`
    : RAW_WANDB_PROJECT
export const WANDB_MODEL = trim(process.env.WANDB_MODEL) || 'meta-llama/Llama-3.1-8B-Instruct'
export const WANDB_BASE_URL = trim(process.env.WANDB_BASE_URL) || 'https://api.inference.wandb.ai/v1'
export const WEAVE_ENABLED = /^(1|true|yes)$/i.test(trim(process.env.WEAVE_ENABLED))

export const NVIDIA_VSS_URL = trim(process.env.NVIDIA_VSS_URL).replace(/\/$/, '')
export const NVIDIA_API_KEY = trim(process.env.NVIDIA_API_KEY)
export const NVIDIA_COSMOS_MODEL = trim(process.env.NVIDIA_COSMOS_MODEL) || 'cosmos-reason2'
export const NVIDIA_NIM_URL = (trim(process.env.NVIDIA_NIM_URL) || 'https://integrate.api.nvidia.com/v1').replace(/\/$/, '')
export const NVIDIA_NIM_MODEL = trim(process.env.NVIDIA_NIM_MODEL) || 'nvidia/cosmos-reason1-7b'
export const NVIDIA_EMBED_URL = trim(process.env.NVIDIA_EMBED_URL).replace(/\/$/, '')
export const NVIDIA_EMBED_MODEL = trim(process.env.NVIDIA_EMBED_MODEL) || 'nvidia/nv-embedqa-e5-v5'

export const GPU_HOST = trim(process.env.GPU_HOST) || '166.19.38.112'
export const GPU_BEARER_TOKEN = trim(process.env.GPU_BEARER_TOKEN)
export const COSMOS3_REASON_URL_SET = trim(process.env.COSMOS3_REASON_URL).replace(/\/$/, '')
export const COSMOS_EMBED1_URL_SET = trim(process.env.COSMOS_EMBED1_URL).replace(/\/$/, '')
export const YOLO_URL_SET = trim(process.env.YOLO_URL).replace(/\/$/, '')
export const COSMOS3_REASON_URL = (COSMOS3_REASON_URL_SET || `http://${GPU_HOST}:8001`).replace(/\/$/, '')
export const YOLO_URL = (YOLO_URL_SET || `http://${GPU_HOST}:8002`).replace(/\/$/, '')
export const COSMOS_EMBED1_URL = (COSMOS_EMBED1_URL_SET || `http://${GPU_HOST}:8003`).replace(/\/$/, '')
export const CANARY_1B_URL = (trim(process.env.CANARY_1B_URL) || `http://${GPU_HOST}:8004`).replace(/\/$/, '')
export const COSMOS3_REASON_MODEL = trim(process.env.COSMOS3_REASON_MODEL) || 'nvidia/cosmos3-reason'
export const COSMOS_EMBED1_MODEL = trim(process.env.COSMOS_EMBED1_MODEL) || 'nvidia/cosmos-embed1'

export const INGRESS_URL = (trim(process.env.INGRESS_URL) || trim(process.env.VSS_URL)).replace(/\/$/, '')
export const VSS_USERNAME = trim(process.env.USERNAME) || trim(process.env.VSS_USERNAME)
export const VSS_PASSWORD = trim(process.env.PASSWORD) || trim(process.env.VSS_PASSWORD)
export const VAST_SEARCH_URL = (trim(process.env.VAST_SEARCH_URL) || INGRESS_URL).replace(/\/$/, '')
export const VAST_API_KEY = trim(process.env.VAST_API_KEY)
export const VAST_UPLOAD = /^(1|true|yes)$/i.test(trim(process.env.VAST_UPLOAD))

export const SEGMENT_DURATION = Number(process.env.SEGMENT_DURATION || 5)
export const VIDEO_DATA_LIMIT = Number(process.env.VIDEO_DATA_LIMIT || 24 * 1024 * 1024)

// Configured-or-off only; verified liveness comes from providers/status.js.
export function modes() {
  const on = (value) => (value ? 'configured' : 'off')
  return {
    cosmos: on(GPU_BEARER_TOKEN || COSMOS3_REASON_URL_SET || NVIDIA_VSS_URL || NVIDIA_API_KEY),
    embed: on(GPU_BEARER_TOKEN || COSMOS_EMBED1_URL_SET || NVIDIA_EMBED_URL || NVIDIA_API_KEY),
    yolo: on(GPU_BEARER_TOKEN || YOLO_URL_SET),
    canary: on(GPU_BEARER_TOKEN && CANARY_1B_URL),
    search: on(INGRESS_URL && VSS_USERNAME && VSS_PASSWORD),
    llm: on(WANDB_API_KEY),
    weave: on(WEAVE_ENABLED && WANDB_API_KEY),
  }
}

export function authHeaders(key) {
  if (!key) return {}
  return { Authorization: `Bearer ${key}` }
}

export function gpuHeaders(json = false) {
  const headers = json ? { 'Content-Type': 'application/json' } : {}
  if (GPU_BEARER_TOKEN) headers.Authorization = `Bearer ${GPU_BEARER_TOKEN}`
  return headers
}
