#!/usr/bin/env python3
"""RoadPlay — real road clip → pixel world synced to YOLO detections + gamification."""

from __future__ import annotations

import os
import re
import threading
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")

app = Flask(__name__)
_token: str | None = None
_token_lock = threading.Lock()

SPRITE_MAP = {
    "person": "ped",
    "bicycle": "bike",
    "motorcycle": "bike",
    "car": "car",
    "truck": "truck",
    "bus": "bus",
    "traffic light": "light",
    "stop sign": "sign",
    "fire hydrant": "prop",
    "dog": "animal",
    "cat": "animal",
}

PLAYABLE = {"car", "truck", "bus", "bike", "ped"}
HAZARD_LABELS = {"truck", "bus"}


def _login(force: bool = False) -> str:
    global _token
    with _token_lock:
        if _token and not force:
            return _token
        r = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        _token = r.json()["access_token"]
        return _token


def _vss(method: str, path: str, **kwargs: Any) -> requests.Response:
    token = _login()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    timeout = kwargs.pop("timeout", 90)
    url = f"{VSS_URL}{path}"
    r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if r.status_code == 401:
        headers["Authorization"] = f"Bearer {_login(force=True)}"
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    return r


def _brief(caption: str) -> str:
    text = re.sub(r"\s+", " ", (caption or "").strip())
    if len(text) > 220:
        text = text[:217] + "..."
    return text or "UNKNOWN ROAD. SCAN FOR MOVING TARGETS."


def _norm_box(bbox: list[Any], shape: list[Any]) -> dict[str, float] | None:
    if not bbox or len(bbox) < 4 or not shape or len(shape) < 2:
        return None
    h, w = float(shape[0]), float(shape[1])
    if h <= 0 or w <= 0:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    cx = ((x1 + x2) * 0.5) / w
    cy = ((y1 + y2) * 0.5) / h
    bw = max(0.02, (x2 - x1) / w)
    bh = max(0.02, (y2 - y1) / h)
    return {
        "x": max(0.0, min(1.0, cx)),
        "y": max(0.0, min(1.0, cy)),
        "w": max(0.02, min(0.45, bw)),
        "h": max(0.02, min(0.45, bh)),
    }


def _build_timeline(det: dict[str, Any], step_sec: float = 0.12, max_per_frame: int = 14) -> dict[str, Any]:
    """Compress YOLO frames into a light timeline the browser can sync to video.currentTime."""
    frames = det.get("frames") or []
    fps = float(det.get("fps") or 30.0)
    shape = det.get("video_shape") or [1080, 1920]
    if isinstance(shape, list) and len(shape) == 2 and all(isinstance(x, (int, float)) for x in shape):
        pass
    else:
        shape = [1080, 1920]

    duration = float(det.get("frame_count") or 0) / fps if fps > 0 else 0.0
    if not duration and frames:
        duration = float(frames[-1].get("time_sec") or 0) + (1.0 / fps)

    samples: list[dict[str, Any]] = []
    next_t = 0.0
    track_seq = 0
    prev_tracks: list[dict[str, Any]] = []

    for fr in frames:
        t = float(fr.get("time_sec") if fr.get("time_sec") is not None else (fr.get("frame_index") or 0) / fps)
        if samples and t + 1e-6 < next_t:
            continue
        dets = list(fr.get("detections") or [])
        dets.sort(key=lambda d: float(d.get("confidence") or 0), reverse=True)
        dets = dets[:max_per_frame]
        used_prev: set[int] = set()
        actors: list[dict[str, Any]] = []
        for d in dets:
            label = str(d.get("label") or "object")
            kind = SPRITE_MAP.get(label, "prop")
            box = _norm_box(d.get("bbox") or [], fr.get("shape") or shape)
            if not box:
                continue
            conf = float(d.get("confidence") or 0)
            best_i, best_dist = -1, 0.08
            for i, p in enumerate(prev_tracks):
                if i in used_prev or p["label"] != label:
                    continue
                dist = abs(p["x"] - box["x"]) + abs(p["y"] - box["y"])
                if dist < best_dist:
                    best_dist, best_i = dist, i
            if best_i >= 0:
                tid = prev_tracks[best_i]["id"]
                used_prev.add(best_i)
            else:
                track_seq += 1
                tid = f"{kind}-{track_seq}"
            actors.append(
                {
                    "id": tid,
                    "kind": kind,
                    "label": label,
                    "conf": round(conf, 3),
                    "x": round(box["x"], 4),
                    "y": round(box["y"], 4),
                    "w": round(box["w"], 4),
                    "h": round(box["h"], 4),
                    "playable": kind in PLAYABLE,
                    "hazard": label in HAZARD_LABELS,
                }
            )
        samples.append({"t": round(t, 3), "actors": actors})
        prev_tracks = [{"id": a["id"], "label": a["label"], "x": a["x"], "y": a["y"]} for a in actors]
        next_t = t + step_sec

    counts = det.get("object_counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    missions = _missions_from_counts(counts)
    return {
        "fps": fps,
        "duration": round(duration, 3),
        "shape": shape,
        "object_counts": counts,
        "object_classes": det.get("object_classes") or list(counts.keys()),
        "detection_count": det.get("detection_count") or 0,
        "samples": samples,
        "missions": missions,
    }


def _missions_from_counts(counts: dict[str, Any]) -> list[dict[str, Any]]:
    missions = []
    cars = int(counts.get("car") or 0) + int(counts.get("truck") or 0) + int(counts.get("bus") or 0)
    peds = int(counts.get("person") or 0)
    bikes = int(counts.get("bicycle") or 0) + int(counts.get("motorcycle") or 0)
    if cars:
        missions.append({"id": "tag_cars", "label": "TAG VEHICLES", "target": min(8, max(2, cars // 2)), "kind": "car"})
    if peds:
        missions.append({"id": "tag_peds", "label": "TAG PEDESTRIANS", "target": min(6, max(1, peds // 2)), "kind": "ped"})
    if bikes:
        missions.append({"id": "tag_bikes", "label": "TAG BIKES", "target": min(3, max(1, bikes)), "kind": "bike"})
    missions.append({"id": "survive", "label": "SURVIVE THE CLIP", "target": 1, "kind": "survive"})
    if not any(m["id"].startswith("tag_") for m in missions):
        missions.insert(0, {"id": "tag_any", "label": "TAG ANY TARGETS", "target": 5, "kind": "any"})
    return missions[:4]


def _fallback_timeline(hit: dict[str, Any]) -> dict[str, Any]:
    """When detections missing, invent slow-moving placeholders from class counts."""
    raw = hit.get("object_classes") or ""
    classes = raw if isinstance(raw, list) else [c.strip() for c in str(raw).split(",") if c.strip()]
    counts: dict[str, int] = {}
    for c in classes:
        counts[c] = counts.get(c, 0) + 1
    actors0 = []
    i = 0
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        kind = SPRITE_MAP.get(label, "prop")
        for j in range(min(n, 5)):
            i += 1
            actors0.append(
                {
                    "id": f"{kind}-{i}",
                    "kind": kind,
                    "label": label,
                    "conf": 0.5,
                    "x": 0.15 + (j % 5) * 0.16,
                    "y": 0.35 + (i % 4) * 0.12,
                    "w": 0.08,
                    "h": 0.08,
                    "playable": kind in PLAYABLE,
                    "hazard": label in HAZARD_LABELS,
                }
            )
    samples = []
    for step in range(40):
        t = step * 0.15
        actors = []
        for a in actors0:
            actors.append(
                {
                    **a,
                    "x": round(min(0.92, a["x"] + step * 0.008 * (1 if hash(a["id"]) % 2 else -1)), 4),
                    "y": a["y"],
                }
            )
        samples.append({"t": round(t, 3), "actors": actors})
    return {
        "fps": 30.0,
        "duration": 6.0,
        "shape": [1080, 1920],
        "object_counts": counts,
        "object_classes": list(counts.keys()),
        "detection_count": 0,
        "samples": samples,
        "missions": _missions_from_counts(counts),
        "fallback": True,
    }


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "roadplay"})


@app.post("/api/search")
def api_search():
    body = request.get_json(force=True, silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    try:
        r = _vss(
            "POST",
            "/api/v1/search",
            json={
                "query": query,
                "top_k": int(body.get("top_k") or 8),
                "min_similarity": float(body.get("min_similarity") or 0.28),
                "metadata_filters": body.get("metadata_filters") or {"capture_type": "streets"},
                "include_public": True,
            },
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("results") or []:
            source = hit.get("source") or ""
            results.append(
                {
                    "source": source,
                    "original_video": hit.get("original_video"),
                    "filename": hit.get("filename"),
                    "camera_id": hit.get("camera_id"),
                    "location": hit.get("location"),
                    "similarity_score": hit.get("similarity_score"),
                    "segment_start_sec": hit.get("segment_start_sec"),
                    "segment_end_sec": hit.get("segment_end_sec"),
                    "reasoning_content": hit.get("reasoning_content") or "",
                    "object_classes": hit.get("object_classes") or "",
                    "stream_path": f"api/stream?source={quote(source, safe='')}" if source else None,
                }
            )
        return jsonify({"count": len(results), "results": results, "query": query})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/world")
def api_world():
    body = request.get_json(force=True, silent=True) or {}
    hit = body.get("hit") or {}
    source = body.get("source") or hit.get("source")
    if not source:
        return jsonify({"error": "source required"}), 400

    meta = dict(hit)
    try:
        mr = _vss("GET", "/api/v1/videos/metadata", params={"source": source})
        if mr.status_code < 400:
            meta.update(mr.json() if isinstance(mr.json(), dict) else {})
            meta["source"] = source
    except Exception:
        pass

    timeline = None
    det_status = None
    try:
        dr = _vss("GET", "/api/v1/videos/detections", params={"source": source}, timeout=120)
        det_status = dr.status_code
        if dr.status_code < 400:
            timeline = _build_timeline(dr.json())
    except Exception as e:
        det_status = str(e)

    if not timeline:
        timeline = _fallback_timeline(meta)

    world = {
        "title": "PIXEL ROAD",
        "brief": _brief(meta.get("reasoning_content") or hit.get("reasoning_content") or ""),
        "source": source,
        "camera_id": meta.get("camera_id") or hit.get("camera_id"),
        "location": meta.get("location") or hit.get("location"),
        "stream_path": f"api/stream?source={quote(source, safe='')}",
        "detections_status": det_status,
        "timeline": timeline,
        "style": "video-to-pixel",
    }
    return jsonify(world)


@app.get("/api/stream")
def api_stream():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "source required"}), 400
    token = _login()
    headers = {}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    upstream = requests.get(
        f"{VSS_URL}/api/v1/videos/stream",
        params={"source": source, "token": token},
        stream=True,
        timeout=120,
        headers=headers,
    )
    if upstream.status_code >= 400:
        return Response(upstream.content, status=upstream.status_code)

    def generate():
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    out = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            out[h] = upstream.headers[h]
    return Response(stream_with_context(generate()), status=upstream.status_code, headers=out, direct_passthrough=True)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>RoadPlay</title>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#1b2133;color:#1c1c24;font-family:"Press Start 2P",monospace;-webkit-font-smoothing:none}
.game{display:grid;grid-template-rows:auto auto 1fr auto;gap:8px;height:100%;padding:10px;overflow:hidden}
.panel{border:3px solid #1c1c24;border-radius:6px;box-shadow:inset 0 0 0 2px #fbf8ec,inset 0 0 0 4px #9aa6c0,0 3px #0d111c;background:#fbf8ec}
.topbar{display:flex;align-items:center;gap:10px;padding:10px 12px;flex-wrap:wrap}
.logo{font-size:13px;letter-spacing:1px;color:#e8353b;text-shadow:2px 2px 0 #1c1c24}
.logo span{margin-left:6px;color:#ffd34d}
.stats{margin-left:auto;display:flex;gap:12px;font-size:7px;color:#4a4a58;flex-wrap:wrap}
.stats b{color:#1c1c24;margin-left:4px}
.searchbar{display:flex;gap:8px;padding:10px;flex-wrap:wrap;align-items:center}
.searchbar input{flex:1;min-width:160px;padding:10px;border:3px solid #1c1c24;background:#fffdf5;font:inherit;font-size:9px}
button{padding:10px 12px;border:3px solid #1c1c24;background:#ffd34d;font:inherit;font-size:9px;cursor:pointer;color:#1c1c24}
button:active{transform:translate(1px,1px)}
button.ghost{background:#e7e3d4}
.stage{display:grid;grid-template-columns:280px 1fr;gap:8px;min-height:0}
@media(max-width:900px){.stage{grid-template-columns:1fr}}
.col{min-height:0;display:flex;flex-direction:column;gap:8px}
.hits{overflow:auto;padding:8px;flex:1;min-height:120px}
.hit{padding:8px;margin-bottom:6px;border:2px solid #1c1c24;background:#fffdf5;cursor:pointer;font-size:7px;line-height:1.45;color:#4a4a58}
.hit:hover,.hit.active{background:#ffe9a8}
.hit strong{color:#1c1c24;display:block;margin-bottom:4px;font-size:8px}
.world-wrap{position:relative;flex:1;min-height:320px;background:#2f3a55;border:3px solid #1c1c24;overflow:hidden}
canvas{width:100%;height:100%;display:block;image-rendering:pixelated}
.hud{position:absolute;left:8px;right:8px;top:8px;display:flex;gap:8px;flex-wrap:wrap;pointer-events:none}
.chip{background:rgba(251,248,236,.92);border:2px solid #1c1c24;padding:6px 8px;font-size:7px;line-height:1.4}
.chip.ok{background:#b8f2c8}.chip.bad{background:#ffb3b8}.chip.hot{background:#ffe9a8}
.bar{height:8px;background:#1c1c24;border:2px solid #1c1c24;margin-top:4px}
.bar>i{display:block;height:100%;background:#ffd34d;width:0%}
.dialogue{padding:10px 12px;font-size:8px;line-height:1.5;color:#1c1c24}
.dialogue .tag{color:#e8353b}
.hint{font-size:7px;color:#4a4a58;margin-top:4px}
video{width:100%;max-height:110px;background:#000;border:2px solid #1c1c24}
.missions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.mission{font-size:7px;padding:4px 6px;border:2px solid #1c1c24;background:#fffdf5}
.mission.done{background:#b8f2c8;text-decoration:line-through}
</style>
</head>
<body>
<div class="game">
  <div class="panel topbar">
    <div class="logo">ROADPLAY <span>VIDEO→PIXEL</span></div>
    <div class="stats">
      <div>SCORE<b id="score">0</b></div>
      <div>COMBO<b id="combo">0</b></div>
      <div>LIVES<b id="lives">3</b></div>
      <div>TAGS<b id="tags">0</b></div>
    </div>
  </div>
  <div class="panel searchbar">
    <input id="q" maxlength="240" placeholder="busy crosswalk with cars and people"/>
    <button id="searchBtn" type="button">SEARCH</button>
    <button class="ghost" id="demoBtn" type="button">DEMO</button>
    <button class="ghost" id="playBtn" type="button">PLAY LEVEL</button>
  </div>
  <div class="stage">
    <div class="panel col">
      <div class="hits" id="hits"><div class="hit">Search → pick a real clip → it becomes the pixel world.</div></div>
      <video id="vid" playsinline muted></video>
    </div>
    <div class="col">
      <div class="world-wrap">
        <canvas id="cv"></canvas>
        <div class="hud">
          <div class="chip" id="syncChip">SYNC --</div>
          <div class="chip" id="modeChip">PIXEL CAM</div>
          <div class="chip hot" id="missionChip">MISSION: SEARCH</div>
        </div>
      </div>
      <div class="panel dialogue">
        <div><span class="tag">BRIEF</span> <span id="brief">Awaiting mission…</span></div>
        <div class="missions" id="missions"></div>
        <div class="hint">World = pixelized video + live YOLO tracks. Arrows/WASD move hero · SPACE tag · P possess nearest car/ped · R restart</div>
        <div class="bar"><i id="prog"></i></div>
      </div>
    </div>
  </div>
</div>
<script>
const BASE = (window.location.pathname.match(/^\/app\/roadplay(?=\/|$)/) || [""])[0];
const api = (p) => `${BASE}/${p.replace(/^\//,"")}`;
const hitsEl = document.getElementById("hits");
const qEl = document.getElementById("q");
const vid = document.getElementById("vid");
const briefEl = document.getElementById("brief");
const missionsEl = document.getElementById("missions");
const scoreEl = document.getElementById("score");
const comboEl = document.getElementById("combo");
const livesEl = document.getElementById("lives");
const tagsEl = document.getElementById("tags");
const syncChip = document.getElementById("syncChip");
const modeChip = document.getElementById("modeChip");
const missionChip = document.getElementById("missionChip");
const prog = document.getElementById("prog");
const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const px = document.createElement("canvas");
const pctx = px.getContext("2d");
px.width = 96; px.height = 54;

let results = [], selected = null, world = null, samples = [];
let score = 0, combo = 0, lives = 3, tags = 0;
let missionProg = {};
let taggedIds = new Set();
let keys = {};
let hero = {x:0.5, y:0.78, possess:null};
let flash = 0, hurtCd = 0, win = false, fail = false;
let lastTagAt = 0;

function esc(s){return String(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
function resize(){
  const r = cv.parentElement.getBoundingClientRect();
  cv.width = Math.max(320, Math.floor(r.width));
  cv.height = Math.max(240, Math.floor(r.height));
}
window.addEventListener("resize", resize); resize();

document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  keys[k] = true;
  if (["arrowup","arrowdown","arrowleft","arrowright"," ","p","r"].includes(k) || e.code==="Space") e.preventDefault();
  if (k==="r") restartLevel();
});
document.addEventListener("keyup", e => { keys[e.key.toLowerCase()] = false; });

async function search(query){
  hitsEl.innerHTML = '<div class="hit">SEARCHING ARCHIVE…</div>';
  const res = await fetch(api("api/search"), {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({query})});
  const data = await res.json();
  if(!res.ok) throw new Error(data.error||"search failed");
  results = data.results||[];
  if(!results.length){ hitsEl.innerHTML = '<div class="hit">NO HITS. TRY ANOTHER QUERY.</div>'; return; }
  hitsEl.innerHTML = results.map((r,i)=>`
    <div class="hit" data-i="${i}">
      <strong>${esc(r.camera_id)} · ${esc(r.location)} · ${(r.similarity_score||0).toFixed(2)}</strong>
      ${esc((r.reasoning_content||"").slice(0,140))}
    </div>`).join("");
  hitsEl.querySelectorAll(".hit").forEach(el => el.addEventListener("click", () => pick(+el.dataset.i)));
  pick(0);
}

async function pick(i){
  selected = results[i];
  hitsEl.querySelectorAll(".hit").forEach((el,idx)=>el.classList.toggle("active", idx===i));
  missionChip.textContent = "LOADING PIXEL WORLD…";
  const res = await fetch(api("api/world"), {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({hit:selected})});
  const data = await res.json();
  if(!res.ok){ briefEl.textContent = data.error||"WORLD FAILED"; return; }
  world = data;
  samples = (data.timeline && data.timeline.samples) || [];
  briefEl.textContent = data.brief;
  if(data.stream_path){
    vid.src = api(data.stream_path);
    vid.load();
  }
  resetGame();
  renderMissions();
  const n = data.timeline?.detection_count || 0;
  const fb = data.timeline?.fallback ? " (fallback)" : "";
  missionChip.textContent = `TRACKS ${n}${fb}`;
  modeChip.textContent = data.timeline?.fallback ? "CLASS FALLBACK" : "PIXEL CAM + YOLO";
}

function resetGame(){
  score=0; combo=0; lives=3; tags=0; win=false; fail=false; flash=0; hurtCd=0;
  taggedIds = new Set();
  missionProg = {};
  (world?.timeline?.missions||[]).forEach(m => missionProg[m.id]=0);
  hero = {x:0.5, y:0.82, possess:null};
  scoreEl.textContent="0"; comboEl.textContent="0"; livesEl.textContent="3"; tagsEl.textContent="0";
  prog.style.width="0%";
}

function restartLevel(){
  resetGame();
  renderMissions();
  try { vid.currentTime = 0; vid.play(); } catch(e){}
}

function renderMissions(){
  const ms = world?.timeline?.missions || [];
  missionsEl.innerHTML = ms.map(m=>{
    const cur = missionProg[m.id]||0;
    const done = m.kind==="survive" ? win : cur>=m.target;
    return `<div class="mission ${done?"done":""}">${esc(m.label)} ${m.kind==="survive"?(done?"OK":"…"):`${Math.min(cur,m.target)}/${m.target}`}</div>`;
  }).join("");
}

function actorsAt(t){
  if(!samples.length) return [];
  let a = samples[0], b = samples[samples.length-1];
  for(let i=0;i<samples.length;i++){
    if(samples[i].t <= t) a = samples[i];
    if(samples[i].t >= t){ b = samples[i]; break; }
  }
  if(a===b || Math.abs(b.t-a.t)<1e-6) return (a.actors||[]).map(x=>({...x}));
  const u = (t - a.t) / (b.t - a.t);
  const mapB = new Map((b.actors||[]).map(x=>[x.id,x]));
  const out = [];
  for(const x of (a.actors||[])){
    const y = mapB.get(x.id);
    if(!y){ out.push({...x}); continue; }
    out.push({
      ...x,
      x: x.x + (y.x-x.x)*u,
      y: x.y + (y.y-x.y)*u,
      w: x.w + (y.w-x.w)*u,
      h: x.h + (y.h-x.h)*u,
    });
  }
  for(const y of (b.actors||[])){
    if(!(a.actors||[]).some(x=>x.id===y.id)) out.push({...y});
  }
  return out;
}

function drawSprite(kind, x, y, w, h, label, tagged){
  const W=cv.width, H=cv.height;
  const px0=x*W, py0=y*H;
  const sw=Math.max(10, w*W*0.9), sh=Math.max(10, h*H*0.9);
  if(tagged){ ctx.globalAlpha = 0.45; }
  if(kind==="hero"){
    ctx.fillStyle="#e8353b"; ctx.fillRect(px0-sw*0.45,py0-sh*0.3,sw*0.9,sh*0.7);
    ctx.fillStyle="#ffd34d"; ctx.fillRect(px0-sw*0.28,py0-sh*0.1,sw*0.56,sh*0.25);
  } else if(kind==="car"||kind==="truck"||kind==="bus"){
    ctx.fillStyle = kind==="bus"?"#5bc0eb": kind==="truck"?"#9b5de5":"#00bbf9";
    ctx.fillRect(px0-sw*0.5,py0-sh*0.35,sw,sh*0.7);
    ctx.fillStyle="#1c1c24"; ctx.fillRect(px0-sw*0.35,py0-sh*0.15,sw*0.7,sh*0.25);
  } else if(kind==="ped"){
    ctx.fillStyle="#f15bb5"; ctx.beginPath(); ctx.arc(px0,py0-sh*0.25,Math.max(4,sw*0.22),0,6.3); ctx.fill();
    ctx.fillRect(px0-sw*0.12,py0-sh*0.05,sw*0.24,sh*0.45);
  } else if(kind==="bike"){
    ctx.strokeStyle="#00f5d4"; ctx.lineWidth=3;
    ctx.beginPath(); ctx.arc(px0-sw*0.25,py0+sh*0.15,sw*0.22,0,6.3); ctx.stroke();
    ctx.beginPath(); ctx.arc(px0+sw*0.25,py0+sh*0.15,sw*0.22,0,6.3); ctx.stroke();
  } else if(kind==="light"){
    ctx.fillStyle="#1c1c24"; ctx.fillRect(px0-4,py0-sh*0.4,8,sh*0.8);
    ctx.fillStyle="#e8353b"; ctx.beginPath(); ctx.arc(px0,py0-sh*0.2,5,0,6.3); ctx.fill();
  } else {
    ctx.fillStyle="#fee440"; ctx.fillRect(px0-sw*0.2,py0-sh*0.2,sw*0.4,sh*0.4);
  }
  ctx.globalAlpha = 1;
  ctx.fillStyle = tagged ? "#b8f2c8" : "#fbf8ec";
  ctx.font = `${Math.max(7, Math.floor(sw*0.18))}px "Press Start 2P"`;
  ctx.fillText(String(label||kind).slice(0,8), px0-sw*0.45, py0-sh*0.45);
}

function drawPixelWorld(actors){
  resize();
  const W=cv.width, H=cv.height;
  ctx.imageSmoothingEnabled = false;

  // Video → simplified pixel backdrop
  if(vid.readyState >= 2){
    pctx.imageSmoothingEnabled = false;
    pctx.drawImage(vid, 0, 0, px.width, px.height);
    // posterize-ish palette punch
    const img = pctx.getImageData(0,0,px.width,px.height);
    const d = img.data;
    for(let i=0;i<d.length;i+=4){
      d[i]   = (d[i]>>5)<<5;
      d[i+1] = (d[i+1]>>5)<<5;
      d[i+2] = (d[i+2]>>5)<<5;
    }
    pctx.putImageData(img,0,0);
    ctx.drawImage(px, 0, 0, W, H);
  } else {
    ctx.fillStyle = "#6ec6ff"; ctx.fillRect(0,0,W,H*0.35);
    ctx.fillStyle = "#3d3d48"; ctx.fillRect(0,H*0.35,W,H*0.65);
  }

  // dim overlay so sprites pop
  ctx.fillStyle = "rgba(27,33,51,0.22)";
  ctx.fillRect(0,0,W,H);

  // scanlines
  ctx.fillStyle="rgba(0,0,0,0.12)";
  for(let y=0;y<H;y+=3) ctx.fillRect(0,y,W,1);

  for(const a of actors){
    drawSprite(a.kind, a.x, a.y, a.w, a.h, a.label, taggedIds.has(a.id));
  }

  // hero / possessed
  if(hero.possess){
    const p = actors.find(a=>a.id===hero.possess);
    if(p){
      hero.x = p.x; hero.y = p.y;
      ctx.strokeStyle="#ffd34d"; ctx.lineWidth=3;
      ctx.strokeRect(p.x*W-p.w*W*0.55, p.y*H-p.h*H*0.45, p.w*W*1.1, p.h*H*0.95);
    } else {
      hero.possess = null;
    }
  }
  if(!hero.possess){
    drawSprite("hero", hero.x, hero.y, 0.06, 0.08, "YOU", false);
  }

  if(flash>0){ ctx.fillStyle=`rgba(255,211,77,${flash})`; ctx.fillRect(0,0,W,H); flash*=0.85; }
  if(win){ ctx.fillStyle="rgba(184,242,200,0.35)"; ctx.fillRect(0,0,W,H); ctx.fillStyle="#1c1c24"; ctx.font="14px Press Start 2P"; ctx.fillText("LEVEL CLEAR", W*0.28, H*0.5); }
  if(fail){ ctx.fillStyle="rgba(232,53,59,0.35)"; ctx.fillRect(0,0,W,H); ctx.fillStyle="#fbf8ec"; ctx.font="14px Press Start 2P"; ctx.fillText("GAME OVER", W*0.3, H*0.5); }
}

function bumpMission(kind){
  const ms = world?.timeline?.missions || [];
  for(const m of ms){
    if(m.kind==="survive") continue;
    if(m.kind==="any" || m.kind===kind || (m.kind==="car" && (kind==="car"||kind==="truck"||kind==="bus"))){
      missionProg[m.id] = (missionProg[m.id]||0) + 1;
    }
  }
  renderMissions();
  checkWin();
}

function checkWin(){
  const ms = world?.timeline?.missions || [];
  const tagsDone = ms.filter(m=>m.kind!=="survive").every(m => (missionProg[m.id]||0) >= m.target);
  if(tagsDone && !win && !fail){
    win = true;
    missionProg.survive = 1;
    score += 500 + combo*50;
    scoreEl.textContent = score;
    renderMissions();
    missionChip.textContent = "LEVEL CLEAR";
    missionChip.className = "chip ok";
  }
}

function step(){
  const t = vid.currentTime || 0;
  const dur = (world?.timeline?.duration) || vid.duration || 1;
  const actors = actorsAt(t);
  syncChip.textContent = `T ${t.toFixed(2)} / ${dur.toFixed(1)} · ${actors.length} MOVING`;
  prog.style.width = `${Math.min(100, (t/Math.max(0.01,dur))*100)}%`;

  // movement
  const speed = 0.012;
  if(!hero.possess){
    if(keys["arrowleft"]||keys["a"]) hero.x = Math.max(0.05, hero.x-speed);
    if(keys["arrowright"]||keys["d"]) hero.x = Math.min(0.95, hero.x+speed);
    if(keys["arrowup"]||keys["w"]) hero.y = Math.max(0.08, hero.y-speed);
    if(keys["arrowdown"]||keys["s"]) hero.y = Math.min(0.92, hero.y+speed);
  }

  // possess
  if(keys["p"]){
    keys["p"]=false;
    if(hero.possess){ hero.possess=null; }
    else {
      let best=null, bd=0.12;
      for(const a of actors){
        if(!a.playable || taggedIds.has(a.id)) continue;
        const dist = Math.hypot(a.x-hero.x, a.y-hero.y);
        if(dist<bd){ bd=dist; best=a; }
      }
      if(best){ hero.possess = best.id; score+=25; scoreEl.textContent=score; flash=0.3; }
    }
  }

  // tag
  if(keys[" "]||keys["space"]){
    keys[" "]=keys["space"]=false;
    const hx = hero.x, hy = hero.y;
    let hit=null, bd=0.09;
    for(const a of actors){
      if(taggedIds.has(a.id) || a.kind==="light" || a.kind==="sign" || a.kind==="prop") continue;
      const dist = Math.hypot(a.x-hx, a.y-hy);
      if(dist<bd){ bd=dist; hit=a; }
    }
    if(hit){
      taggedIds.add(hit.id);
      const now = performance.now();
      combo = (now - lastTagAt < 1800) ? combo+1 : 1;
      lastTagAt = now;
      const pts = (hit.hazard?120:70) * Math.max(1, combo);
      score += pts; tags += 1;
      scoreEl.textContent=score; comboEl.textContent=combo; tagsEl.textContent=tags;
      bumpMission(hit.kind);
      flash=0.4;
      if(hero.possess===hit.id) hero.possess=null;
    }
  }

  // hazard collisions
  if(hurtCd>0) hurtCd--;
  if(!fail && !win && hurtCd<=0){
    for(const a of actors){
      if(!a.hazard || taggedIds.has(a.id)) continue;
      if(Math.hypot(a.x-hero.x, a.y-hero.y) < 0.055){
        lives = Math.max(0, lives-1);
        livesEl.textContent = lives;
        hurtCd = 45;
        combo = 0; comboEl.textContent="0";
        flash = 0.5;
        if(lives<=0){ fail=true; missionChip.textContent="GAME OVER — R TO RETRY"; missionChip.className="chip bad"; try{vid.pause();}catch(e){} }
        break;
      }
    }
  }

  // auto-complete survive if video ended with lives
  if(!win && !fail && vid.ended && lives>0){
    const ms = world?.timeline?.missions || [];
    const tagsDone = ms.filter(m=>m.kind!=="survive").every(m => (missionProg[m.id]||0) >= m.target);
    if(tagsDone) checkWin();
    else { missionChip.textContent = "CLIP OVER — FINISH TAGS (R)"; missionChip.className="chip hot"; }
  }

  drawPixelWorld(actors);
  requestAnimationFrame(step);
}
requestAnimationFrame(step);

document.getElementById("searchBtn").onclick = () => search(qEl.value.trim()||"busy intersection with cars and pedestrians").catch(e=>hitsEl.innerHTML=`<div class="hit">ERROR: ${esc(e.message)}</div>`);
document.getElementById("demoBtn").onclick = () => { qEl.value="pedestrians and cars at a city crosswalk"; document.getElementById("searchBtn").click(); };
document.getElementById("playBtn").onclick = () => { restartLevel(); vid.play().catch(()=>{}); };
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


def main():
    try:
        _login()
        print("roadplay: VSS login ok", flush=True)
    except Exception as e:
        print(f"roadplay: VSS login deferred ({e})", flush=True)
    try:
        import gunicorn.app.base

        class Standalone(gunicorn.app.base.BaseApplication):
            def __init__(self, application, options=None):
                self.application = application
                self.options = options or {}
                super().__init__()

            def load_config(self):
                for k, v in self.options.items():
                    self.cfg.set(k.lower(), v)

            def load(self):
                return self.application

        Standalone(app, {"bind": f"0.0.0.0:{PORT}", "workers": 1, "threads": 4, "timeout": 120}).run()
    except Exception:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
