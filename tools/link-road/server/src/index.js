import { serve } from '@hono/node-server'
import { serveStatic } from '@hono/node-server/serve-static'
import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { bodyLimit } from 'hono/body-limit'
import fs from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { answerQuestion, fuseIncident, runAgent } from './agent.js'
import { HOST, ROOT, SEGMENT_DURATION, TEAM_CONFIG, modes, PORT } from './config.js'
import { WEAVE_ENABLED, WANDB_PROJECT, WANDB_API_KEY } from './config.js'
import { classify } from './incident.js'
import { ROAD_PROMPT } from './prompts.js'
import { understand } from './providers/cosmos.js'
import { embed } from './providers/embed.js'
import { loadClipBytes } from './providers/media.js'
import { ingest as remoteIngest, query as searchQuery, randomObstructions } from './providers/search.js'
import { transcribe } from './providers/canary.js'
import { verifyStack } from './providers/status.js'
import { vssStreamTarget } from './providers/vss.js'
import { catalog, frameAt, yolo11 } from './providers/yolo.js'
import {
  clipPath,
  getClip,
  listClips,
  listSegments,
  publicClip,
  replaceSegments,
  saveClipFile,
  upsertClip,
} from './store.js'
import { initWeave, listTraces, record } from './trace.js'

const app = new Hono()

app.use('*', cors())
app.use(
  '*',
  bodyLimit({
    maxSize: 80 * 1024 * 1024,
    onError: (c) => c.json({ error: 'FILE TOO LARGE' }, 413),
  }),
)

function id(prefix) {
  return `${prefix}-${crypto.randomBytes(4).toString('hex')}`
}

function clipType(file) {
  const mime = file.type || ''
  if (mime.startsWith('video')) return 'video'
  if (mime.startsWith('image')) return 'image'
  if (/\.(mp4|webm|mov|m4v)$/i.test(file.name || '')) return 'video'
  return 'image'
}

function segmentsFor(duration, type) {
  if (type === 'image' || !duration || duration <= 0) return [{ start: 0, end: 0 }]
  const span = duration
  const chunks = []
  for (let start = 0; start < span; start += SEGMENT_DURATION) {
    chunks.push({ start, end: Math.min(span, start + SEGMENT_DURATION) })
  }
  return chunks.length ? chunks : [{ start: 0, end: span }]
}

app.get('/health', (c) => c.json({ ok: true }))

app.get('/api/health', async (c) => {
  const stack = await verifyStack(c.req.query('force') === '1')
  return c.json({
    ok: Object.values(stack.modes).every((m) => m === 'live'),
    modes: stack.modes,
    errors: stack.errors,
    vm: Boolean(TEAM_CONFIG || process.env.INGRESS_URL || process.env.VSS_URL),
    time: Date.now(),
  })
})

app.get('/api/traces', (c) => c.json({ traces: listTraces() }))

app.post('/api/transcribe', async (c) => {
  const body = await c.req.parseBody()
  const file = body.file
  if (!file || typeof file === 'string') return c.json({ error: 'AUDIO REQUIRED' }, 400)
  try {
    const buffer = Buffer.from(await file.arrayBuffer())
    const result = await transcribe(buffer, file.name || 'speech.wav')
    return c.json(result)
  } catch (error) {
    record('canary.transcribe', { error: error.message }, { status: 'error' })
    return c.json({ error: `CANARY FAILED: ${String(error.message).toUpperCase().slice(0, 120)}` }, 502)
  }
})

app.get('/api/clips', async (c) => {
  const clips = await listClips()
  return c.json({ clips: clips.map(publicClip) })
})

app.get('/api/clips/:id/file', async (c) => {
  const clip = await getClip(c.req.param('id'))
  if (!clip) return c.json({ error: 'NOT FOUND' }, 404)
  const data = await fs.readFile(clipPath(clip))
  return new Response(data, {
    status: 200,
    headers: {
      'Content-Type': clip.mime || 'application/octet-stream',
      'Content-Disposition': `inline; filename="${clip.name}"`,
    },
  })
})

app.post('/api/ingest', async (c) => {
  const body = await c.req.parseBody()
  const file = body.file
  if (!file || typeof file === 'string') return c.json({ error: 'FILE REQUIRED' }, 400)
  const buffer = Buffer.from(await file.arrayBuffer())
  const clipId = id('clip')
  const type = clipType(file)
  const duration = Number(body.duration || 0)
  const saved = await saveClipFile(clipId, file.name, buffer)
  const clip = await upsertClip({
    id: clipId,
    name: file.name || 'FOOTAGE',
    type,
    mime: file.type,
    duration,
    stored: saved.stored,
    createdAt: Date.now(),
    videoId: null,
  })
  record('ingest.save', { clipId, name: clip.name, bytes: buffer.length })

  const filePayload = { buffer, name: file.name, type: file.type }
  let cosmos
  try {
    cosmos = await understand({ file: filePayload, prompt: ROAD_PROMPT })
    if (cosmos.videoId) await upsertClip({ ...clip, videoId: cosmos.videoId })
  } catch (error) {
    record('ingest.cosmos', { error: error.message }, { status: 'error' })
    return c.json({ clip: publicClip(clip), error: `COSMOS: ${error.message}` }, 502)
  }

  const windows = segmentsFor(duration, type)
  const segments = []
  const captionVector = await embed(cosmos.caption || cosmos.summary || clip.name).catch((error) => error)
  if (captionVector instanceof Error) {
    return c.json({ clip: publicClip(clip), cosmos, error: `COSMOS EMBED: ${captionVector.message}` }, 502)
  }
  for (const window of windows) {
    const caption =
      windows.length === 1
        ? cosmos.caption || cosmos.summary
        : `${window.start}–${window.end}s of ${clip.name}. ${cosmos.caption || cosmos.summary || ''}`.trim()
    const vector = captionVector
    segments.push({
      id: id('seg'),
      clipId,
      start: window.start,
      end: window.end,
      caption,
      summary: cosmos.summary || caption,
      vector,
      detections: [],
      thumbnail: null,
      provider: cosmos.provider,
      name: clip.name,
    })
  }
  await replaceSegments(clipId, segments)
  const remote = await remoteIngest(clip, filePayload)
  return c.json({
    clip: publicClip(clip),
    segments: segments.map(({ vector, ...rest }) => rest),
    cosmos,
    search: remote,
  })
})

app.post('/api/scan', async (c) => {
  const body = await c.req.json()
  const clipId = body.clipId
  const time = Number(body.time || 0)
  const duration = Number(body.duration || 0)
  let fused
  let yolo = null
  try {
    const clip = await loadClipBytes({ clipId, source: body.source })
    const isVideo = clip && !String(clip.type).startsWith('image')
    let detections = body.detections || []
    let detector = 'yolov8n-browser'
    const vision = []
    if (isVideo) vision.push(yolo11(clip).then((result) => ({ kind: 'yolo', result })))
    if (clip) {
      vision.push(
        understand({
          file: clip,
          prompt: ROAD_PROMPT,
          frames: body.frame ? [body.frame] : [],
          time,
        }).then((cosmos) => ({ kind: 'cosmos', cosmos })),
      )
    }
    const parts = await Promise.all(vision)
    const yoloResult = parts.find((p) => p.kind === 'yolo')?.result
    const cosmos = parts.find((p) => p.kind === 'cosmos')?.cosmos
    if (yoloResult) {
      const frame = frameAt(yoloResult, time, duration)
      detections = frame.detections
      detector = 'yolo11'
      yolo = {
        model: 'yolo11',
        frames: yoloResult.frames.length,
        detections,
        w: frame.w,
        h: frame.h,
        catalog: catalog(yoloResult.counts, yoloResult.frames.length),
      }
    }
    fused = await fuseIncident({
      detections,
      caption: body.caption,
      time,
      frames: body.frame ? [body.frame] : [],
      file: clip || undefined,
      detector,
      cosmos,
      expectedKind: body.expectedKind,
    })
  } catch (error) {
    record('scan', { error: error.message }, { status: 'error' })
    return c.json({ error: String(error.message || 'SCAN FAILED').toUpperCase().slice(0, 120) }, 502)
  }
  const detections = yolo?.detections || body.detections || []
  if (clipId && !/^s3:\/\//i.test(clipId)) {
    const clip = await getClip(clipId)
    const segs = await listSegments(clipId)
    const match =
      segs.find((s) => time >= s.start && time <= (s.end || s.start + SEGMENT_DURATION)) || segs[0]
    if (match) {
      const caption = fused.cosmos?.caption || fused.incident.summary || match.caption
      const vector = await embed(caption).catch(() => match.vector)
      const next = segs.map((s) =>
        s.id === match.id
          ? {
              ...s,
              caption,
              summary: fused.incident.summary || caption,
              vector,
              detections,
              thumbnail: body.frame || s.thumbnail,
              incident: fused.incident,
            }
          : s,
      )
      await replaceSegments(clipId, next)
    }
    if (clip) await upsertClip({ ...clip, scannedAt: Date.now() })
  }
  return c.json({
    incident: fused.incident,
    cosmos: fused.cosmos,
    yolo: yolo || { model: 'yolov8n-browser', detections, ...classify(detections) },
    provider: fused.provider,
  })
})

app.get('/api/archive/stream', async (c) => {
  const source = c.req.query('source')
  if (!source) return c.json({ error: 'SOURCE REQUIRED' }, 400)
  try {
    const target = await vssStreamTarget(source)
    const headers = {}
    const range = c.req.header('Range')
    if (range) headers.Range = range
    const upstream = await fetch(target.url, { headers, signal: AbortSignal.timeout(30000) })
    const upstreamType = upstream.headers.get('content-type') || ''
    const out = {
      'Content-Type': upstreamType.startsWith('video/') ? upstreamType : 'video/mp4',
    }
    const length = upstream.headers.get('content-length')
    const ranges = upstream.headers.get('accept-ranges')
    const contentRange = upstream.headers.get('content-range')
    if (length) out['Content-Length'] = length
    if (ranges) out['Accept-Ranges'] = ranges
    if (contentRange) out['Content-Range'] = contentRange
    return new Response(upstream.body, { status: upstream.status, headers: out })
  } catch {
    return c.json({ error: 'ARCHIVE STREAM FAILED' }, 502)
  }
})

app.post('/api/search', async (c) => {
  const body = await c.req.json()
  const query = String(body.query || '').trim()
  if (!query) return c.json({ hits: [] })
  try {
    const result = await searchQuery(query, Number(body.limit || 8))
    const hits = Array.isArray(result) ? result : result.hits || []
    return c.json({
      hits,
      query,
      synthesis: result.synthesis || '',
      issue: result.issue || null,
      label: result.label || '',
      unresolved: Boolean(result.unresolved),
    })
  } catch (error) {
    return c.json({ error: `SEARCH FAILED: ${String(error.message).toUpperCase().slice(0, 100)}` }, 502)
  }
})

app.get('/api/archive/obstructions', async (c) => {
  try {
    const limit = Number(c.req.query('limit') || 4)
    const result = await randomObstructions(Math.min(8, Math.max(1, limit)))
    return c.json(result)
  } catch (error) {
    return c.json({ error: `OBSTRUCTION SEARCH FAILED: ${String(error.message).toUpperCase().slice(0, 100)}` }, 502)
  }
})

app.post('/api/ask', async (c) => {
  const body = await c.req.json()
  const question = String(body.question || '').trim()
  if (!question) return c.json({ error: 'QUESTION REQUIRED' }, 400)
  const clipId = body.clipId || null
  const source = body.source || null
  const local = clipId && !/^s3:\/\//i.test(clipId)
  const clip = local ? await getClip(clipId) : null
  const segs = local ? await listSegments(clipId) : []
  const captions = segs.map((s) => s.caption).filter(Boolean)
  try {
    const file = await loadClipBytes({ clipId, source }).catch(() => null)
    const result = await answerQuestion({ clipId, question, captions, videoId: clip?.videoId, file })
    return c.json({ ...result, clipId })
  } catch (error) {
    record('ask', { error: error.message }, { status: 'error' })
    return c.json({ error: `ASK FAILED: ${String(error.message).toUpperCase().slice(0, 100)}` }, 502)
  }
})

app.post('/api/agent', async (c) => {
  const body = await c.req.json()
  const text = String(body.text || '').trim()
  if (!text) return c.json({ action: 'error', text: 'EMPTY COMMAND' })
  const result = await runAgent(text, body.snapshot || {})
  return c.json(result)
})

if (WEAVE_ENABLED && WANDB_PROJECT && WANDB_API_KEY) {
  initWeave(WANDB_PROJECT).catch((error) => {
    console.error('weave init failed:', error.message)
  })
}

const DIST = path.join(ROOT, 'web/dist')
if (existsSync(DIST)) {
  const rel = path.relative(process.cwd(), DIST) || 'web/dist'
  app.use('/*', serveStatic({ root: rel }))
  app.get('*', async (c) => {
    const html = await fs.readFile(path.join(DIST, 'index.html'), 'utf8')
    return c.html(html)
  })
}

const server = serve({ fetch: app.fetch, port: PORT, hostname: HOST }, (info) => {
  record('server.listen', { port: info.port, modes: modes() })
  const where = TEAM_CONFIG ? 'vm-config' : 'local-env'
  const ui = existsSync(DIST) ? ' + ui' : ''
  console.log(`link-road api on http://0.0.0.0:${info.port}  ${where}${ui}  ${JSON.stringify(modes())}`)
})

server.on('error', (error) => {
  if (error.code === 'EADDRINUSE') {
    console.error(`port ${PORT} already in use. stop the other api (lsof -i :${PORT}) and retry.`)
    process.exit(0)
  }
  throw error
})
