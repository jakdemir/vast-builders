import { CANARY_1B_URL, GPU_BEARER_TOKEN, gpuHeaders } from '../config.js'
import { withTrace } from '../trace.js'

export function canaryMode() {
  return GPU_BEARER_TOKEN && CANARY_1B_URL ? 'configured' : 'off'
}

function transcriptOf(data) {
  if (!data || typeof data !== 'object') return ''
  const text =
    data.text ||
    data.transcript ||
    data.transcription ||
    data.output ||
    data.hypotheses?.[0]?.transcript ||
    data.choices?.[0]?.text ||
    data.choices?.[0]?.message?.content
  return String(text || '').trim()
}

export async function transcribe(buffer, filename = 'speech.wav') {
  return withTrace(
    'canary.transcribe',
    async () => {
      const form = new FormData()
      form.set('file', new File([buffer], filename, { type: 'audio/wav' }))
      form.set('language', 'en-US')
      const response = await fetch(`${CANARY_1B_URL}/v1/audio/transcriptions`, {
        method: 'POST',
        headers: gpuHeaders(),
        body: form,
        signal: AbortSignal.timeout(30000),
      })
      const raw = await response.text()
      let data = {}
      try {
        data = raw ? JSON.parse(raw) : {}
      } catch {
        data = { text: raw }
      }
      if (!response.ok) {
        const detail = data.detail || data.error || raw.slice(0, 80)
        throw new Error(`CANARY ${response.status}: ${detail}`)
      }
      const text = transcriptOf(data)
      return { text, provider: 'canary-1b' }
    },
    { bytes: buffer?.length || 0, name: filename },
  )
}
