import fs from 'node:fs/promises'
import { clipPath, getClip } from '../store.js'
import { vssStreamTarget } from './vss.js'

const isS3 = (value) => /^s3:\/\//i.test(String(value || ''))

// The GPU host cannot resolve the VAST S3 hostname, so clips are always fetched here and sent inline.
export async function loadClipBytes({ clipId, source } = {}) {
  if (isS3(source) || isS3(clipId)) {
    const uri = isS3(source) ? source : clipId
    const target = await vssStreamTarget(uri)
    const response = await fetch(target.url, { signal: AbortSignal.timeout(60000) })
    if (!response.ok) throw new Error(`ARCHIVE CLIP ${response.status}`)
    const buffer = Buffer.from(await response.arrayBuffer())
    return { buffer, type: 'video/mp4', name: uri.split('/').pop() || 'clip.mp4', origin: 'vast' }
  }
  if (clipId) {
    const clip = await getClip(clipId)
    if (!clip) throw new Error('CLIP NOT FOUND')
    const buffer = await fs.readFile(clipPath(clip))
    return { buffer, type: clip.mime || 'video/mp4', name: clip.name, origin: 'local' }
  }
  return null
}
