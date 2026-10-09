import { applyExpectedKind, classify, normalizeIncident } from './incident.js'
import { AGENT_SYSTEM, FUSION_SYSTEM, ROAD_PROMPT } from './prompts.js'
import { askVideo, understand } from './providers/cosmos.js'
import { complete, llmMode } from './providers/llm.js'
import { vssAsk, vssConfigured } from './providers/vss.js'

const CARS = [
  { id: 'blue', words: ['blue', 'sedan'], short: 'BLUE' },
  { id: 'silver', words: ['silver', 'grey', 'gray', 'hatch', 'hatchback'], short: 'SILVER' },
  { id: 'truck', words: ['red', 'truck', 'lorry'], short: 'TRUCK' },
  { id: 'cab', words: ['yellow', 'cab', 'taxi'], short: 'CAB' },
  { id: 'van', words: ['green', 'van'], short: 'VAN' },
  { id: 'suv', words: ['black', 'suv'], short: 'SUV' },
]

const ITEMS = [
  { kind: 'pothole', re: /\bpot ?holes?\b/, label: 'POTHOLE', scope: 'road' },
  { kind: 'debris', re: /\b(debris|tires?|tyres?|junk|wreck(age)?)\b/, label: 'DEBRIS', scope: 'road' },
  { kind: 'speed', re: /\b(speed (limit|meter|sign|camera|trap)|radar)\b/, label: 'SPD METER', scope: 'road' },
  { kind: 'police', re: /\b(police|cops?|patrol)\b/, label: 'POLICE', scope: 'lane' },
  { kind: 'slow', re: /\bslow (car|vehicle|driver|truck)\b/, label: 'SLOW CAR', scope: 'lane' },
]

const NUMBERS = {
  1: 0, one: 0, first: 0, '1st': 0,
  2: 1, two: 1, second: 1, '2nd': 1,
  3: 2, three: 2, third: 2, '3rd': 2,
  4: 3, four: 3, fourth: 3, '4th': 3,
}

const LANES = 4
const has = (text, word) => new RegExp(`\\b${word}\\b`).test(text)

function laneIn(text) {
  const named = text.match(/\blane (\w+)\b/) || text.match(/\b(\w+) lane\b/)
  if (named && named[1] in NUMBERS) return NUMBERS[named[1]]
  if (/\b(leftmost|far left|fast lane)\b/.test(text)) return 0
  if (/\b(rightmost|far right|slow lane|last lane)\b/.test(text)) return LANES - 1
  return null
}

const normalize = (text) =>
  ` ${String(text || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim()} `

function parseRules(text) {
  const raw = normalize(text)
  const lane = laneIn(raw)

  if (
    /\b(find|search|show|where|locate|look for|list)\b/.test(raw) ||
    /\b(unresolved|outstanding|open)\b/.test(raw) ||
    /\bissues?\b/.test(raw)
  ) {
    return { action: 'search', query: String(text || '').trim(), reason: 'keyword search' }
  }
  if (/\b(was|were|did|does|anyone|what happened|ask|crosswalk|before)\b/.test(raw) || raw.includes('?')) {
    return { action: 'ask', question: String(text || '').trim(), reason: 'keyword ask' }
  }

  const car = CARS.find((c) => c.words.some((w) => has(raw, w)))
  if (car) {
    if (lane == null) return { action: 'error', text: `WHICH LANE SHOULD ${car.short} TAKE?` }
    if (lane < 0 || lane >= LANES) return { action: 'error', text: `THERE ARE ONLY ${LANES} LANES.` }
    return { action: 'lane', car: car.id, lane, reason: 'regex lane' }
  }

  if (/\b(footage|video|clip|photo|image|upload)\b/.test(raw)) {
    return { action: 'footage', lane: lane ?? 1, reason: 'regex footage' }
  }

  const hazard = ITEMS.find((h) => h.re.test(raw))
  if (hazard) {
    return { action: 'hazard', item: { kind: hazard.kind, label: hazard.label, scope: hazard.scope }, lane: lane ?? 1, reason: 'regex hazard' }
  }

  const thing = raw.match(/^ (?:there s |there is |theres |a |an |the |some )*(.+?) (?:in|on) (?:the )?(?:lane \w+|\w+ lane)/)
  if (thing && lane != null) {
    const label = thing[1].toUpperCase().slice(0, 10).trim()
    return { action: 'hazard', item: { kind: 'custom-road', label, scope: 'road' }, lane, reason: 'regex custom' }
  }

  return { action: 'error', text: "I DIDN'T CATCH THAT. TRY 'RED TRUCK TO LANE 1' OR 'FIND A PEDESTRIAN'." }
}

// Rules only correct fields the LLM is known to get wrong; the LLM always decides the action.
function correct(parsed, text) {
  const raw = normalize(text)
  const hint = parseRules(text)
  // The LLM often echoes the user's 1-based lane number despite the 0-based prompt.
  const lane = laneIn(raw) ?? parsed.lane
  const car = CARS.some((c) => c.id === parsed.car) ? parsed.car : hint.car
  const item = parsed.action === 'hazard' && hint.item ? { ...hint.item, ...(parsed.item || {}) } : parsed.item
  if (hint.action === 'search') {
    return { ...parsed, action: 'search', query: hint.query || parsed.query || text, lane, car, item }
  }
  return { ...parsed, lane, car, item }
}

export async function runAgent(text, snapshot = {}) {
  if (llmMode() !== 'live') return { action: 'error', text: 'AGENT LLM IS NOT CONFIGURED.', provider: 'off' }
  try {
    let result = await complete({
      system: AGENT_SYSTEM,
      user: `Command: ${text}\nSnapshot: ${JSON.stringify(snapshot).slice(0, 2000)}`,
      json: true,
    })
    if (!result.json?.action) {
      result = await complete({
        system: `${AGENT_SYSTEM}\nYour previous reply was not valid JSON. Reply with the JSON object only.`,
        user: `Command: ${text}`,
        json: true,
      })
    }
    if (!result.json?.action) return { action: 'error', text: "AGENT COULDN'T PARSE THAT. TRY AGAIN.", provider: result.provider }
    return { ...correct(result.json, text), text: result.json.text || text, provider: result.provider }
  } catch (error) {
    return { action: 'error', text: `AGENT LLM ERROR: ${String(error.message).slice(0, 60)}`, provider: 'wandb' }
  }
}

export async function fuseIncident({ detections, caption, time, frames, file, detector = 'yolo11', cosmos: prior, expectedKind }) {
  const yolo = classify(detections || [])
  const cosmos =
    prior ||
    (await understand({
      detections,
      time,
      prompt: ROAD_PROMPT,
      frames: frames || [],
      file,
    }))
  const summary = cosmos.summary || cosmos.caption || caption || ''
  const result = await complete({
    system: FUSION_SYSTEM,
    user: `${detector.toUpperCase()} detections: ${JSON.stringify((detections || []).map((d) => ({ label: d.label, score: Number(d.score.toFixed(2)) })).slice(0, 60))}\nCosmos video reasoning: ${summary}\nTime: ${time ?? 0}${expectedKind ? `\nExpected archive kind: ${expectedKind}. Keep this type unless the video clearly contradicts it.` : ''}`,
    json: true,
  })
  if (!result.json) throw new Error('FUSION LLM RETURNED NO JSON')
  const incident = applyExpectedKind(normalizeIncident(result.json, { ...yolo, summary }), expectedKind)
  incident.provider = result.provider
  incident.summary = incident.summary || summary
  return { incident, cosmos: { ...cosmos, summary }, provider: result.provider }
}

export async function answerQuestion({ clipId, question, captions, videoId, file }) {
  if (file?.buffer) {
    const cosmos = await understand({
      file,
      prompt: `${question}\nAnswer from this road video. Name people, vehicles, and what they are doing. One or two short sentences.`,
    })
    return {
      answer: cosmos.caption,
      provider: `cosmos-${cosmos.provider}`,
      input: cosmos.input,
      model: cosmos.model,
    }
  }
  if (/^s3:\/\//i.test(String(clipId || ''))) return vssAsk(question, clipId)
  if (clipId && captions?.length) return askVideo({ videoId, question, captions })
  if (vssConfigured()) return vssAsk(question, null)
  throw new Error('NO FOOTAGE TO ANSWER FROM')
}
