import fs from 'node:fs/promises'
import path from 'node:path'
import { CLIPS_DIR, DATA_DIR, STORE_PATH, publicPath } from './config.js'

const empty = () => ({ clips: [], segments: [] })

let memory = empty()
let ready = null

async function ensure() {
  if (ready) return ready
  ready = (async () => {
    await fs.mkdir(CLIPS_DIR, { recursive: true })
    try {
      const raw = await fs.readFile(STORE_PATH, 'utf8')
      const parsed = JSON.parse(raw)
      memory = {
        clips: Array.isArray(parsed.clips) ? parsed.clips : [],
        segments: Array.isArray(parsed.segments) ? parsed.segments : [],
      }
    } catch {
      memory = empty()
      await persist()
    }
  })()
  return ready
}

async function persist() {
  await fs.mkdir(DATA_DIR, { recursive: true })
  await fs.writeFile(STORE_PATH, JSON.stringify(memory, null, 2))
}

export async function loadStore() {
  await ensure()
  return memory
}

export async function saveClipFile(id, filename, buffer) {
  await ensure()
  const safe = String(filename || 'clip.bin').replace(/[^a-zA-Z0-9._-]+/g, '_')
  const stored = `${id}-${safe}`
  const dest = path.join(CLIPS_DIR, stored)
  await fs.writeFile(dest, buffer)
  return { stored, path: dest }
}

export function clipPath(clip) {
  return path.join(CLIPS_DIR, clip.stored)
}

export async function upsertClip(clip) {
  await ensure()
  const index = memory.clips.findIndex((item) => item.id === clip.id)
  if (index >= 0) memory.clips[index] = { ...memory.clips[index], ...clip }
  else memory.clips.push(clip)
  await persist()
  return memory.clips.find((item) => item.id === clip.id)
}

export async function replaceSegments(clipId, segments) {
  await ensure()
  memory.segments = memory.segments.filter((item) => item.clipId !== clipId).concat(segments)
  await persist()
  return segments
}

export async function patchSegment(id, patch) {
  await ensure()
  const index = memory.segments.findIndex((item) => item.id === id)
  if (index < 0) return null
  memory.segments[index] = { ...memory.segments[index], ...patch }
  await persist()
  return memory.segments[index]
}

export async function listClips() {
  await ensure()
  return memory.clips
}

export async function getClip(id) {
  await ensure()
  return memory.clips.find((item) => item.id === id) || null
}

export async function listSegments(clipId) {
  await ensure()
  return clipId ? memory.segments.filter((item) => item.clipId === clipId) : memory.segments
}

export async function allSegments() {
  await ensure()
  return memory.segments
}

export function publicClip(clip) {
  if (!clip) return null
  return {
    id: clip.id,
    name: clip.name,
    type: clip.type,
    duration: clip.duration,
    createdAt: clip.createdAt,
    url: publicPath(`/api/clips/${clip.id}/file`),
  }
}
