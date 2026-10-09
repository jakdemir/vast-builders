import OpenAI from 'openai'
import { WANDB_API_KEY, WANDB_BASE_URL, WANDB_MODEL, WANDB_PROJECT } from '../config.js'
import { getWeave, initWeave, withTrace } from '../trace.js'

function extractJson(text) {
  if (!text) return null
  const cleaned = String(text).replace(/<think>[\s\S]*?<\/think>/gi, '')
  const fence = cleaned.match(/```(?:json)?\s*([\s\S]*?)```/)
  const raw = fence ? fence[1] : cleaned
  const start = raw.indexOf('{')
  const end = raw.lastIndexOf('}')
  if (start < 0 || end <= start) return null
  try {
    return JSON.parse(raw.slice(start, end + 1))
  } catch {
    return null
  }
}

export function llmMode() {
  return WANDB_API_KEY ? 'live' : 'off'
}

let cached = null

async function client() {
  if (!WANDB_API_KEY) return null
  if (cached) return cached
  await initWeave()
  const raw = new OpenAI({
    apiKey: WANDB_API_KEY,
    baseURL: WANDB_BASE_URL,
    project: WANDB_PROJECT || undefined,
    defaultHeaders: WANDB_PROJECT ? { 'OpenAI-Project': WANDB_PROJECT } : undefined,
  })
  const weave = getWeave()
  cached = weave?.wrapOpenAI ? weave.wrapOpenAI(raw) : raw
  return cached
}

export async function complete({ system, user, json = false }) {
  return withTrace(
    'llm.complete',
    async () => {
      const llm = await client()
      if (!llm) throw new Error('WANDB_API_KEY NOT SET')
      const response = await llm.chat.completions.create({
        model: WANDB_MODEL,
        temperature: 0.2,
        max_tokens: 600,
        messages: [
          { role: 'system', content: system },
          { role: 'user', content: user },
        ],
      })
      const text = response.choices?.[0]?.message?.content || ''
      return { text, json: json ? extractJson(text) : null, provider: 'wandb' }
    },
    { model: WANDB_MODEL, json, project: WANDB_PROJECT },
  )
}
