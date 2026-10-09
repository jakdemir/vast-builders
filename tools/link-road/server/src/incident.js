const VEHICLES = new Set(['car', 'truck', 'bus', 'motorcycle', 'train'])
const ANIMALS = new Set(['bird', 'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe'])
const SIGNALS = new Set(['traffic light', 'stop sign'])
const TYPES = new Set(['clear', 'crash', 'animal', 'crowd', 'stalled', 'cyclist', 'pedestrian', 'pothole', 'debris', 'object', 'unknown'])
const MODELS = new Set(['crash', 'animal', 'people', 'bike', 'person', 'car', 'crate', 'pothole'])

function overlaps(a, b) {
  const pad = 0.1 * Math.min(a.w, b.w)
  return a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + a.h + pad && b.y < a.y + b.h + pad
}

export function tally(dets) {
  const counts = new Map()
  for (const d of dets) counts.set(d.label, (counts.get(d.label) || 0) + 1)
  return [...counts]
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => `${n} ${label.toUpperCase()}${n > 1 && !label.endsWith('s') ? 'S' : ''}`)
    .join(', ')
}

const short = (text) => String(text || 'OBJECT').toUpperCase().slice(0, 10)

export function classify(detections = []) {
  const dets = detections.filter((d) => !SIGNALS.has(d.label))
  if (!dets.length) {
    return { type: 'clear', label: 'ALL CLEAR', blocks: false, seen: 'NOTHING ON THE ROAD', confidence: 0 }
  }

  const holes = dets.filter((d) => d.label === 'pothole')
  const people = dets.filter((d) => d.label === 'person')
  const animals = dets.filter((d) => ANIMALS.has(d.label))
  const vehicles = dets.filter((d) => VEHICLES.has(d.label))
  const bikes = dets.filter((d) => d.label === 'bicycle')
  const seen = tally(dets)
  const top = (list) => Math.max(...list.map((d) => d.score))
  const touching = vehicles.some((a, i) => vehicles.some((b, j) => j > i && overlaps(a, b)))

  if (holes.length) {
    return { type: 'pothole', label: 'POTHOLE', scope: 'road', blocks: true, caution: true, model: 'pothole', seen, confidence: top(holes) }
  }
  if (vehicles.length >= 2 && touching) {
    return { type: 'crash', label: 'CRASH', scope: 'road', blocks: true, caution: true, model: 'crash', seen, confidence: top(vehicles) }
  }
  if (animals.length) {
    const lead = animals.sort((a, b) => b.score - a.score)[0]
    return { type: 'animal', label: short(lead.label), scope: 'road', blocks: true, caution: false, model: 'animal', seen, confidence: lead.score }
  }
  if (people.length >= 3) {
    return { type: 'crowd', label: 'PEOPLE', scope: 'road', blocks: true, caution: true, model: 'people', seen, confidence: top(people) }
  }
  if (people.length) {
    return { type: 'pedestrian', label: 'PEDESTRIAN', scope: 'road', blocks: true, caution: true, model: 'person', seen, confidence: top(people) }
  }
  if (bikes.length) {
    return { type: 'cyclist', label: 'CYCLIST', scope: 'lane', blocks: true, caution: true, model: 'bike', seen, confidence: top(bikes) }
  }
  if (vehicles.length) {
    return { type: 'stalled', label: 'STALLED', scope: 'lane', blocks: true, caution: false, model: 'car', seen, confidence: top(vehicles) }
  }
  const lead = dets.sort((a, b) => b.score - a.score)[0]
  return { type: 'object', label: short(lead.label), scope: 'lane', blocks: true, caution: false, model: 'crate', seen, confidence: lead.score }
}

export const UNREADABLE = {
  type: 'unknown',
  label: 'OBJECT',
  scope: 'lane',
  blocks: true,
  caution: false,
  model: 'crate',
  seen: 'FOOTAGE UNREADABLE',
  confidence: 0,
}

export function normalizeIncident(raw, fallback) {
  if (!raw || typeof raw !== 'object') return fallback
  const type = TYPES.has(raw.type) ? raw.type : fallback.type
  const model = MODELS.has(raw.model) ? raw.model : fallback.model
  const scope = raw.scope === 'road' || raw.scope === 'lane' ? raw.scope : fallback.scope
  const confidence = Number(raw.confidence)
  return {
    ...fallback,
    type,
    label: short(raw.label || fallback.label),
    blocks: type === 'clear' ? false : true,
    seen: String(raw.seen || fallback.seen || '').toUpperCase(),
    confidence: Number.isFinite(confidence) ? Math.min(1, Math.max(0, confidence)) : fallback.confidence,
    scope: type === 'clear' ? undefined : scope,
    caution: Boolean(raw.caution ?? fallback.caution),
    model,
    summary: String(raw.summary || fallback.summary || '').slice(0, 240),
    provider: raw.provider || fallback.provider,
  }
}

export function applyExpectedKind(incident, expected) {
  const next = incident && typeof incident === 'object' ? { ...incident } : { ...UNREADABLE }
  if (expected === 'pothole') {
    return { ...next, type: 'pothole', label: 'POTHOLE', scope: 'road', model: 'pothole', caution: true, blocks: true }
  }
  if (expected === 'debris') {
    return { ...next, type: 'debris', label: 'DEBRIS', scope: 'road', model: 'crate', caution: true, blocks: true }
  }
  if (expected === 'pedestrian') {
    return { ...next, type: 'pedestrian', label: 'PEDESTRIAN', scope: 'road', model: 'person', caution: true, blocks: true }
  }
  return next
}
