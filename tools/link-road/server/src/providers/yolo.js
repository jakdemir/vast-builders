import { YOLO_URL, gpuHeaders } from '../config.js'
import { withTrace } from '../trace.js'

const ROAD = new Set(['person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck', 'dog', 'horse', 'cow', 'deer'])

function toBox(d) {
  const [x1, y1, x2, y2] = d.bbox || [0, 0, 0, 0]
  return { label: d.label, score: Number(d.confidence || 0), x: x1, y: y1, w: x2 - x1, h: y2 - y1 }
}

function score(dets) {
  return dets.reduce((sum, d) => sum + (ROAD.has(d.label) ? 1 + d.score : 0.15 * d.score), 0)
}

export async function yolo11({ buffer, name }) {
  return withTrace(
    'yolo11.infer',
    async () => {
      const response = await fetch(`${YOLO_URL}/v1/infer`, {
        method: 'POST',
        headers: gpuHeaders(true),
        body: JSON.stringify({
          video_base64: buffer.toString('base64'),
          filename: name || 'clip.mp4',
          include_frames: false,
        }),
        signal: AbortSignal.timeout(90000),
      })
      if (!response.ok) throw new Error(`YOLO11 ${response.status}`)
      const data = await response.json()
      if (!data.perception_ok) throw new Error('YOLO11 PERCEPTION FAILED')
      const frames = (data.frames || []).map((f) => ({
        index: f.frame_index,
        h: f.shape?.[0] || 0,
        w: f.shape?.[1] || 0,
        detections: (f.detections || []).map(toBox),
      }))
      if (!frames.length) {
        const detections = []
        for (const [label, n] of Object.entries(data.object_counts || {})) {
          const count = Math.max(1, Math.min(6, Math.round(Number(n) || 1)))
          for (let i = 0; i < count; i += 1) detections.push({ label, score: 0.7, x: 0, y: 0, w: 10, h: 10 })
        }
        frames.push({ index: 0, h: 0, w: 0, detections })
      }
      return { counts: data.object_counts || {}, classes: data.object_classes || [], frames }
    },
    { name },
  )
}

// Pick the YOLO11 frame matching the browser's snapshot time so boxes line up with the shown frame.
export function frameAt(result, time, duration) {
  const frames = result.frames
  if (!frames.length) return { detections: [], w: 0, h: 0 }
  if (Number.isFinite(time) && duration > 0) {
    const i = Math.min(frames.length - 1, Math.max(0, Math.round((time / duration) * (frames.length - 1))))
    return frames[i]
  }
  return frames.reduce((best, f) => (score(f.detections) > score(best.detections) ? f : best), frames[0])
}

export function catalog(counts, frameCount) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  if (!frameCount || frameCount <= 1) {
    return entries.slice(0, 8).map(([label, n]) => `${Math.round(n)} ${label.toUpperCase()}`).join(', ')
  }
  return entries
    .map(([label, n]) => [label, n / frameCount])
    .filter(([, avg]) => avg >= 0.1)
    .map(([label, avg]) => `${avg.toFixed(1)} ${label.toUpperCase()}/FRAME`)
    .join(', ')
}
