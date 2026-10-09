import { WANDB_API_KEY, WANDB_PROJECT, WEAVE_ENABLED } from './config.js'

const MAX = 80
const traces = []

export function record(name, detail = {}, extra = {}) {
  const step = {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
    t: Date.now(),
    name,
    ...extra,
    detail,
  }
  traces.unshift(step)
  if (traces.length > MAX) traces.length = MAX
  return step
}

export async function withTrace(name, fn, meta = {}) {
  const started = Date.now()
  record(name, meta, { status: 'start' })
  try {
    const result = await fn()
    record(name, { ...meta, ms: Date.now() - started }, { status: 'ok' })
    return result
  } catch (error) {
    record(name, { ...meta, error: error.message, ms: Date.now() - started }, { status: 'error' })
    throw error
  }
}

export function listTraces(limit = 24) {
  return traces.slice(0, limit)
}

let weaveMod = null
let weaveClient = null
let weaveTried = false
let weaveReady = null

export function getWeave() {
  return weaveMod
}

export async function initWeave(project = WANDB_PROJECT) {
  if (weaveReady) return weaveReady
  weaveReady = (async () => {
    if (weaveTried) return weaveClient
    weaveTried = true
    if (!WEAVE_ENABLED || !WANDB_API_KEY || !project) return null
    try {
      weaveMod = await import('weave')
      await weaveMod.login(WANDB_API_KEY)
      weaveClient = await weaveMod.init(project)
      record('weave.init', { project }, { status: 'ok' })
      return weaveClient
    } catch (error) {
      record('weave.init', { error: error.message }, { status: 'error' })
      weaveMod = null
      weaveClient = null
      return null
    }
  })()
  return weaveReady
}
