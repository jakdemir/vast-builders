#!/usr/bin/env python3
"""RoadPlay — real road clip → flowing cartoon pixel world + arcade gamification."""

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
    if len(text) > 200:
        text = text[:197] + "..."
    return text or "CHAOS STREET. CATCH THE CREW BEFORE THE CLIP ENDS!"


def _norm_box(bbox: list[Any], shape: list[Any]) -> dict[str, float] | None:
    if not bbox or len(bbox) < 4 or not shape or len(shape) < 2:
        return None
    h, w = float(shape[0]), float(shape[1])
    if h <= 0 or w <= 0:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    cx = ((x1 + x2) * 0.5) / w
    cy = ((y1 + y2) * 0.5) / h
    bw = max(0.03, (x2 - x1) / w)
    bh = max(0.03, (y2 - y1) / h)
    return {
        "x": max(0.0, min(1.0, cx)),
        "y": max(0.0, min(1.0, cy)),
        "w": max(0.03, min(0.4, bw)),
        "h": max(0.03, min(0.4, bh)),
    }


def _missions_from_counts(counts: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    cars = int(counts.get("car") or 0) + int(counts.get("truck") or 0) + int(counts.get("bus") or 0)
    peds = int(counts.get("person") or 0)
    bikes = int(counts.get("bicycle") or 0) + int(counts.get("motorcycle") or 0)
    missions = [
        {
            "id": "rush",
            "label": "STREET RUSH",
            "blurb": "Tag anything moving — fill the fever bar!",
            "target": min(12, max(5, (cars + peds + bikes) // 2 or 5)),
            "kind": "any",
            "reward": 400,
        }
    ]
    if cars:
        missions.append(
            {
                "id": "tag_cars",
                "label": "WHEEL GRAB",
                "blurb": "Tag cars / trucks / buses",
                "target": min(8, max(3, cars // 2)),
                "kind": "car",
                "reward": 300,
            }
        )
    if peds:
        missions.append(
            {
                "id": "tag_peds",
                "label": "SIDEWALK CREW",
                "blurb": "Tag pedestrians",
                "target": min(7, max(2, peds // 2)),
                "kind": "ped",
                "reward": 300,
            }
        )
    if bikes:
        missions.append(
            {
                "id": "tag_bikes",
                "label": "TWO-WHEEL ZOOM",
                "blurb": "Tag bikes / scooters",
                "target": min(4, max(1, bikes)),
                "kind": "bike",
                "reward": 250,
            }
        )
    missions.append(
        {
            "id": "fever",
            "label": "FEVER x5",
            "blurb": "Hit a 5-chain combo",
            "target": 5,
            "kind": "combo",
            "reward": 500,
        }
    )
    missions.append(
        {
            "id": "survive",
            "label": "DONT CRASH",
            "blurb": f"Stay alive for the {duration:.0f}s clip",
            "target": 1,
            "kind": "survive",
            "reward": 600,
        }
    )
    return missions[:6]


def _build_timeline(det: dict[str, Any], step_sec: float = 0.1, max_per_frame: int = 16) -> dict[str, Any]:
    frames = det.get("frames") or []
    fps = float(det.get("fps") or 30.0)
    shape = det.get("video_shape") or [1080, 1920]
    if not (isinstance(shape, list) and len(shape) == 2):
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
            best_i, best_dist = -1, 0.09
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
                    "star": conf > 0.75 and kind in PLAYABLE,
                }
            )
        samples.append({"t": round(t, 3), "actors": actors})
        prev_tracks = [{"id": a["id"], "label": a["label"], "x": a["x"], "y": a["y"]} for a in actors]
        next_t = t + step_sec

    counts = det.get("object_counts") if isinstance(det.get("object_counts"), dict) else {}
    return {
        "fps": fps,
        "duration": round(duration, 3),
        "shape": shape,
        "object_counts": counts,
        "object_classes": det.get("object_classes") or list(counts.keys()),
        "detection_count": det.get("detection_count") or 0,
        "samples": samples,
        "missions": _missions_from_counts(counts, duration),
    }


def _fallback_timeline(hit: dict[str, Any]) -> dict[str, Any]:
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
                    "w": 0.09,
                    "h": 0.09,
                    "playable": kind in PLAYABLE,
                    "hazard": label in HAZARD_LABELS,
                    "star": True,
                }
            )
    samples = []
    for step in range(50):
        t = step * 0.12
        actors = []
        for a in actors0:
            drift = step * 0.01
            actors.append(
                {
                    **a,
                    "x": round(0.08 + (a["x"] + drift) % 0.84, 4),
                    "y": round(a["y"] + 0.02 * ((step % 10) / 10), 4),
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
        "missions": _missions_from_counts(counts, 6.0),
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

    return jsonify(
        {
            "title": "ROADPLAY",
            "brief": _brief(meta.get("reasoning_content") or hit.get("reasoning_content") or ""),
            "source": source,
            "camera_id": meta.get("camera_id") or hit.get("camera_id"),
            "location": meta.get("location") or hit.get("location"),
            "stream_path": f"api/stream?source={quote(source, safe='')}",
            "detections_status": det_status,
            "timeline": timeline,
            "style": "link-road-cartoon",
        }
    )


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
.game{display:grid;grid-template-rows:auto auto 1fr;gap:8px;height:100%;padding:10px;overflow:hidden;user-select:none}
.panel{border:3px solid #1c1c24;border-radius:6px;box-shadow:inset 0 0 0 2px #fbf8ec,inset 0 0 0 4px #9aa6c0,0 3px #0d111c;background:#fbf8ec}
.topbar{display:flex;align-items:center;gap:12px;padding:10px 14px;flex-wrap:wrap}
.logo{font-size:14px;letter-spacing:1px;color:#e8353b;text-shadow:2px 2px 0 #1c1c24}
.logo span{margin-left:6px;color:#ffd34d}
.stats{margin-left:auto;display:flex;gap:14px;font-size:8px;color:#4a4a58;flex-wrap:wrap}
.stats b{color:#1c1c24;margin-left:4px}
.searchbar{display:flex;gap:8px;padding:10px;flex-wrap:wrap;align-items:center;background:#2b3350;box-shadow:inset 0 0 0 2px #56628c,0 3px #0d111c;border:3px solid #1c1c24;border-radius:6px}
.searchbar input{flex:1;min-width:160px;padding:10px;border:0;background:transparent;color:#fbf8ec;font:inherit;font-size:9px;text-transform:uppercase;outline:none}
.searchbar input::placeholder{color:#7f8bb2}
button{padding:8px 10px;border:2px solid #1c1c24;border-radius:4px;background:#ffd34d;font:inherit;font-size:9px;cursor:pointer;box-shadow:0 2px #1c1c24;color:#1c1c24}
button:active{transform:translateY(2px);box-shadow:none}
button.ghost{background:#e7e3d4}
button.go{background:#9ff6ff}
.stage{display:grid;grid-template-columns:260px 1fr;gap:8px;min-height:0}
@media(max-width:900px){.stage{grid-template-columns:1fr}}
.col{min-height:0;display:flex;flex-direction:column;gap:8px}
.hits{overflow:auto;padding:8px;flex:1;min-height:100px}
.hit{padding:8px;margin-bottom:6px;border:2px solid #1c1c24;border-radius:4px;background:#fffdf5;cursor:pointer;font-size:7px;line-height:1.45;color:#4a4a58}
.hit:hover,.hit.active{background:#ffe9a8}
.hit strong{color:#1c1c24;display:block;margin-bottom:4px;font-size:8px}
.world-wrap{position:relative;flex:1;min-height:340px;background:#cfe4ec;border:3px solid #1c1c24;border-radius:6px;overflow:hidden;box-shadow:inset 0 0 0 2px #fbf8ec,inset 0 0 0 4px #9aa6c0,0 3px #0d111c}
canvas{width:100%;height:100%;display:block;image-rendering:pixelated}
.hud{position:absolute;left:10px;right:10px;top:10px;display:flex;gap:8px;flex-wrap:wrap;pointer-events:none;z-index:2}
.chip{background:#141826b8;border:2px solid #1c1c24;border-radius:4px;padding:6px 8px;font-size:7px;color:#fbf8ec}
.chip.ok{background:#2f9e5acc}.chip.bad{background:#e84838cc}.chip.hot{background:#b07a00cc}
.feverbar{position:absolute;left:12%;right:12%;bottom:12px;height:12px;border:2px solid #1c1c24;border-radius:3px;background:#3a3a48;overflow:hidden;z-index:2}
.feverbar>i{display:block;height:100%;width:0%;background:linear-gradient(90deg,#0b8794,#ffd34d,#e8353b)}
.toast{position:absolute;left:50%;top:40%;transform:translate(-50%,-50%);font-size:14px;color:#ffd34d;text-shadow:3px 3px 0 #1c1c24;pointer-events:none;opacity:0;z-index:3;transition:opacity .15s}
.toast.show{opacity:1}
.dialogue{padding:12px 14px;font-size:9px;line-height:1.7}
.dialogue .tag{color:#e8353b}
.hint{font-size:7px;color:#6a6a78;margin-top:6px}
video{width:100%;max-height:96px;background:#11141c;border:2px solid #1c1c24;border-radius:3px}
.missions{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.mission{font-size:7px;padding:6px;border:2px solid #1c1c24;border-radius:3px;background:#e7e3d4;line-height:1.35}
.mission.done{background:#b8f0c4}
.mission .n{color:#0b8794}
.stars{color:#ffd34d;font-size:12px;margin-top:6px;letter-spacing:2px}
.legend{position:absolute;left:10px;bottom:28px;display:flex;gap:10px;padding:6px 8px;font-size:6px;color:#fbf8ec;background:#141826b8;border:2px solid #1c1c24;border-radius:4px;z-index:2;pointer-events:none}
.legend i{display:inline-block;width:8px;height:8px;border:2px solid #1c1c24;margin-right:4px;vertical-align:middle}
</style>
</head>
<body>
<div class="game">
  <div class="panel topbar">
    <div class="logo">ROADPLAY <span>CARTOON ROAD</span></div>
    <div class="stats">
      <div>SCORE<b id="score">0</b></div>
      <div>COMBO<b id="combo">0</b></div>
      <div>LIVES<b id="lives">5</b></div>
      <div>STARS<b id="stars">0</b></div>
    </div>
  </div>
  <div class="searchbar">
    <span style="color:#ffd34d;font-size:10px">›</span>
    <input id="q" maxlength="240" placeholder="find a busy street with cars and people"/>
    <button id="searchBtn" type="button">SEARCH</button>
    <button class="ghost" id="demoBtn" type="button">LUCKY</button>
    <button class="go" id="playBtn" type="button">GO</button>
  </div>
  <div class="stage">
    <div class="panel col">
      <div class="hits" id="hits"><div class="hit">Search a real clip — we rebuild it as a Link Road–style cartoon stage.</div></div>
      <video id="vid" playsinline muted></video>
    </div>
    <div class="col">
      <div class="world-wrap">
        <canvas id="cv"></canvas>
        <div class="hud">
          <div class="chip" id="syncChip">READY</div>
          <div class="chip hot" id="powerChip">POWER NONE</div>
          <div class="chip" id="missionChip">FIND A CLIP</div>
        </div>
        <div class="legend"><span><i style="background:#3ff2ff"></i>YOU</span><span><i style="background:#ffd34d"></i>STAR</span><span><i style="background:#e8353b"></i>HAZARD</span></div>
        <div class="feverbar"><i id="feverFill"></i></div>
        <div class="toast" id="toast"></div>
      </div>
      <div class="panel dialogue">
        <div><span class="tag">BRIEF</span> <span id="brief">Awaiting mission…</span></div>
        <div class="missions" id="missions"></div>
        <div class="stars" id="starRate"></div>
        <div class="hint">WASD move · SPACE tag · P possess · F fever · R retry · real clip clocks the movers</div>
      </div>
    </div>
  </div>
</div>
<script>
const BASE = (window.location.pathname.match(/^\/app\/roadplay(?=\/|$)/) || [""])[0];
const api = (p) => `${BASE}/${p.replace(/^\//,"")}`;
const $ = (id) => document.getElementById(id);
const hitsEl=$("hits"), qEl=$("q"), vid=$("vid"), briefEl=$("brief"), missionsEl=$("missions");
const scoreEl=$("score"), comboEl=$("combo"), livesEl=$("lives"), starsEl=$("stars");
const syncChip=$("syncChip"), powerChip=$("powerChip"), missionChip=$("missionChip");
const feverFill=$("feverFill"), toastEl=$("toast"), starRate=$("starRate");
const cv=$("cv"), ctx=cv.getContext("2d");

let results=[], world=null, samples=[];
let score=0, combo=0, lives=5, starCount=0, fever=0, maxCombo=0;
let missionProg={}, taggedIds=new Set(), popped=new Set();
let keys={}, hero={x:.5,y:.86,vx:0,vy:0,possess:null};
let particles=[], floats=[], clouds=[];
let power=null, powerT=0, shake=0, flash=0, hurtCd=0;
let win=false, fail=false, lastTagAt=0, tAnim=0;
let smoothActors=new Map();
let dashOffset=0;

// Link Road palette
const C = {
  skyTop:"#6fb3e8", skyBot:"#cfe4ec", grass:"#6aa84f", grassDark:"#629f48",
  road:"#4a4e56", roadMid:"#5a5e66", roadEdge:"#43474f",
  line:"#f2f2ec", yellow:"#f3c331", curb:"#c9c6bc",
  build:"#aeb5bd", buildDark:"#1f2b38", ink:"#1c1c24", cream:"#fbf8ec",
  red:"#e8353b", cyan:"#3ff2ff", teal:"#0b8794", amber:"#ffd34d"
};
const PAL = {car:"#9ad7ff",truck:"#ff7ad9",bus:"#58d068",ped:"#ffd34d",bike:"#3ff2ff",light:"#e8353b",hero:"#3ff2ff",prop:"#ffae1a",sign:"#ff3b30",animal:"#ffae1a"};

function esc(s){return String(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
function toast(msg,ms=900){ toastEl.textContent=msg; toastEl.classList.add("show"); clearTimeout(toast._t); toast._t=setTimeout(()=>toastEl.classList.remove("show"),ms); }
function beep(freq=520,dur=0.05,type="square",vol=0.03){
  try{ const AC=window.AudioContext||window.webkitAudioContext; if(!AC) return; if(!beep.ac) beep.ac=new AC();
    const o=beep.ac.createOscillator(), g=beep.ac.createGain(); o.type=type; o.frequency.value=freq; g.gain.value=vol;
    o.connect(g); g.connect(beep.ac.destination); o.start(); g.gain.exponentialRampToValueAtTime(0.0001, beep.ac.currentTime+dur); o.stop(beep.ac.currentTime+dur);
  }catch(e){}
}
function resize(){ const r=cv.parentElement.getBoundingClientRect(); cv.width=Math.max(360,Math.floor(r.width)); cv.height=Math.max(260,Math.floor(r.height)); }
window.addEventListener("resize", resize); resize();
for(let i=0;i<6;i++) clouds.push({x:Math.random(), y:0.06+Math.random()*0.16, s:0.08+Math.random()*0.1, v:0.00015+Math.random()*0.00025});

document.addEventListener("keydown", e=>{
  const k=e.key.toLowerCase(); keys[k]=true;
  if(["arrowup","arrowdown","arrowleft","arrowright"," ","p","r","f"].includes(k)||e.code==="Space") e.preventDefault();
  if(k==="r") restartLevel();
  if(k==="f") triggerFever();
});
document.addEventListener("keyup", e=> keys[e.key.toLowerCase()]=false);

async function search(query){
  hitsEl.innerHTML='<div class="hit">SEARCHING…</div>';
  const res=await fetch(api("api/search"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query})});
  const data=await res.json(); if(!res.ok) throw new Error(data.error||"search failed");
  results=data.results||[];
  if(!results.length){ hitsEl.innerHTML='<div class="hit">NO HITS. TRY LUCKY.</div>'; return; }
  hitsEl.innerHTML=results.map((r,i)=>`<div class="hit" data-i="${i}"><strong>${esc(r.camera_id)} · ${esc(r.location)} · ${(r.similarity_score||0).toFixed(2)}</strong>${esc((r.reasoning_content||"").slice(0,130))}</div>`).join("");
  hitsEl.querySelectorAll(".hit").forEach(el=>el.addEventListener("click",()=>pick(+el.dataset.i)));
  pick(0);
}

async function pick(i){
  hitsEl.querySelectorAll(".hit").forEach((el,idx)=>el.classList.toggle("active",idx===i));
  missionChip.textContent="BUILDING ROAD…";
  const res=await fetch(api("api/world"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({hit:results[i]})});
  const data=await res.json(); if(!res.ok){ briefEl.textContent=data.error||"fail"; return; }
  world=data; samples=(data.timeline&&data.timeline.samples)||[];
  briefEl.textContent=data.brief;
  if(data.stream_path){ vid.src=api(data.stream_path); vid.load(); }
  resetGame(); renderMissions();
  missionChip.textContent=`CARTOON · ${data.timeline?.detection_count||0} TRACKS`;
  toast("ROAD READY",1000); beep(660,.08);
}

function resetGame(){
  score=0; combo=0; lives=5; starCount=0; fever=0; maxCombo=0;
  missionProg={}; (world?.timeline?.missions||[]).forEach(m=>missionProg[m.id]=0);
  taggedIds=new Set(); popped=new Set(); particles=[]; floats=[];
  power=null; powerT=0; shake=0; flash=0; hurtCd=0; win=false; fail=false;
  hero={x:.5,y:.86,vx:0,vy:0,possess:null}; smoothActors=new Map();
  scoreEl.textContent="0"; comboEl.textContent="0"; livesEl.textContent="5"; starsEl.textContent="0";
  feverFill.style.width="0%"; starRate.textContent=""; powerChip.textContent="POWER NONE"; missionChip.className="chip";
}
function restartLevel(){ resetGame(); renderMissions(); try{vid.currentTime=0; vid.playbackRate=1; vid.play();}catch(e){} toast("RETRY",600); }

function renderMissions(){
  const ms=world?.timeline?.missions||[];
  missionsEl.innerHTML=ms.map(m=>{
    const cur=missionProg[m.id]||0;
    const done=m.kind==="survive"?win:(m.kind==="combo"?maxCombo>=m.target:cur>=m.target);
    const show=m.kind==="survive"?(done?"CLEAR":"LIVE"):(m.kind==="combo"?`${Math.min(maxCombo,m.target)}/${m.target}`:`${Math.min(cur,m.target)}/${m.target}`);
    return `<div class="mission ${done?"done":""}"><b>${esc(m.label)}</b><br><span class="n">${show}</span> · ${esc(m.blurb||"")}</div>`;
  }).join("");
}

function actorsAt(t){
  if(!samples.length) return [];
  let a=samples[0], b=samples[samples.length-1];
  for(let i=0;i<samples.length;i++){ if(samples[i].t<=t) a=samples[i]; if(samples[i].t>=t){ b=samples[i]; break; } }
  if(a===b||Math.abs(b.t-a.t)<1e-6) return (a.actors||[]).map(x=>({...x}));
  const u=(t-a.t)/(b.t-a.t); const s=u*u*(3-2*u);
  const mapB=new Map((b.actors||[]).map(x=>[x.id,x])); const out=[];
  for(const x of (a.actors||[])){
    const y=mapB.get(x.id);
    if(!y){ out.push({...x}); continue; }
    out.push({...x, x:x.x+(y.x-x.x)*s, y:x.y+(y.y-x.y)*s, w:x.w+(y.w-x.w)*s, h:x.h+(y.h-x.h)*s});
  }
  for(const y of (b.actors||[])) if(!(a.actors||[]).some(x=>x.id===y.id)) out.push({...y});
  return out;
}
function softActors(raw){
  const next=new Map();
  for(const a of raw){
    const prev=smoothActors.get(a.id);
    if(!prev) next.set(a.id,{...a});
    else { const k=0.32; next.set(a.id,{...a, x:prev.x+(a.x-prev.x)*k, y:prev.y+(a.y-prev.y)*k, w:prev.w+(a.w-prev.w)*k, h:prev.h+(a.h-prev.h)*k}); }
  }
  smoothActors=next; return [...next.values()];
}

/** Map detection bbox center into cartoon road perspective (Link Road vibe). */
function toRoad(nx, ny){
  const depth = Math.max(0.05, Math.min(0.98, ny)); // farther = higher in frame-ish; use as depth
  // put nearer objects lower on canvas
  const z = 0.22 + depth * 0.72;
  const spread = 0.22 + depth * 0.5;
  const x = 0.5 + (nx - 0.5) * spread * 1.35;
  return {x: Math.max(0.06, Math.min(0.94, x)), y: Math.max(0.28, Math.min(0.94, z)), scale: 0.55 + depth * 0.9};
}

function spawnBurst(x,y,color,n=10){ for(let i=0;i<n;i++) particles.push({x,y,vx:(Math.random()-.5)*.018,vy:(Math.random()-.8)*.02,life:28+Math.random()*18,color,r:2+Math.random()*3}); }
function floatText(x,y,text,color="#ffd34d"){ floats.push({x,y,text,color,life:40}); }

function drawCartoonStage(W,H){
  // sky
  const g=ctx.createLinearGradient(0,0,0,H*0.45);
  g.addColorStop(0,C.skyTop); g.addColorStop(1,C.skyBot);
  ctx.fillStyle=g; ctx.fillRect(0,0,W,H);

  // clouds drifting
  ctx.fillStyle="rgba(255,255,255,0.85)";
  for(const cl of clouds){
    cl.x += cl.v; if(cl.x>1.2) cl.x=-0.2;
    const cx=cl.x*W, cy=cl.y*H, s=cl.s*W;
    ctx.beginPath(); ctx.arc(cx,cy,s*0.35,0,6.3); ctx.arc(cx+s*0.3,cy-s*0.1,s*0.28,0,6.3); ctx.arc(cx+s*0.55,cy,s*0.32,0,6.3); ctx.fill();
  }

  // far hills / buildings silhouette
  ctx.fillStyle=C.build;
  for(let i=0;i<14;i++){
    const bw=W*(0.06+((i*17)%5)*0.015), bh=H*(0.08+((i*29)%6)*0.03);
    const bx=((i*0.09 + dashOffset*0.0002)%1.1)*W - bw*0.2;
    ctx.fillRect(bx, H*0.30-bh, bw, bh);
    ctx.fillStyle=C.buildDark; ctx.fillRect(bx+4, H*0.30-bh+6, Math.max(3,bw*0.25), Math.max(4,bh*0.35));
    ctx.fillStyle=C.build;
  }

  // grass bands
  ctx.fillStyle=C.grass; ctx.fillRect(0,H*0.32,W,H*0.68);
  ctx.fillStyle=C.grassDark;
  for(let i=0;i<20;i++){ ctx.fillRect((i*47 + dashOffset*0.4)%W, H*0.34+(i%5)*H*0.1, 18, 6); }

  // road trapezoid (perspective)
  const topY=H*0.36, botY=H;
  const topL=W*0.38, topR=W*0.62, botL=W*0.05, botR=W*0.95;
  ctx.fillStyle=C.road;
  ctx.beginPath(); ctx.moveTo(topL,topY); ctx.lineTo(topR,topY); ctx.lineTo(botR,botY); ctx.lineTo(botL,botY); ctx.closePath(); ctx.fill();

  // road edges
  ctx.strokeStyle=C.yellow; ctx.lineWidth=4;
  ctx.beginPath(); ctx.moveTo(topL,topY); ctx.lineTo(botL,botY); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(topR,topY); ctx.lineTo(botR,botY); ctx.stroke();

  // flowing lane dashes
  dashOffset = (dashOffset + 2.2) % 40;
  ctx.strokeStyle=C.line; ctx.lineWidth=3; ctx.setLineDash([16,18]); ctx.lineDashOffset=-dashOffset;
  for(const t of [0.33,0.5,0.67]){
    const x0=topL+(topR-topL)*t, x1=botL+(botR-botL)*t;
    ctx.beginPath(); ctx.moveTo(x0,topY+8); ctx.lineTo(x1,botY); ctx.stroke();
  }
  ctx.setLineDash([]);

  // curb strips
  ctx.fillStyle=C.curb;
  ctx.fillRect(0, H*0.32, W, 4);

  // trees along sides (simple Link-ish blobs)
  for(let i=0;i<8;i++){
    const side = i%2===0?-1:1;
    const depth = 0.4 + (i%4)*0.12;
    const x = side<0 ? W*(0.08+depth*0.12) : W*(0.92-depth*0.12);
    const y = H*(0.4 + depth*0.45);
    const s = 10 + depth*22;
    ctx.fillStyle=C.ink; ctx.fillRect(x-3, y, 6, s*0.8);
    ctx.fillStyle=C.grassDark; ctx.beginPath(); ctx.arc(x,y,s*0.7,0,6.3); ctx.fill();
    ctx.fillStyle=C.grass; ctx.beginPath(); ctx.arc(x-s*0.25,y-s*0.1,s*0.45,0,6.3); ctx.arc(x+s*0.2,y-s*0.15,s*0.4,0,6.3); ctx.fill();
  }
}

function drawActor(a, road){
  const W=cv.width, H=cv.height;
  const bob=Math.sin(tAnim*0.15 + a.x*30)*2;
  const px=road.x*W, py=road.y*H+bob;
  const s=Math.min(W,H)*0.045*road.scale;
  const tagged=taggedIds.has(a.id);
  const col=PAL[a.kind]||PAL.prop;
  ctx.save();
  if(tagged) ctx.globalAlpha=0.35;
  // shadow
  ctx.fillStyle="rgba(24,25,29,0.35)";
  ctx.beginPath(); ctx.ellipse(px, py+s*0.55, s*0.55, s*0.18, 0,0,6.3); ctx.fill();

  if(a.kind==="ped"||a.kind==="animal"){
    ctx.fillStyle=col;
    ctx.fillRect(px-s*0.25, py-s*0.1, s*0.5, s*0.7);
    ctx.beginPath(); ctx.arc(px, py-s*0.25, s*0.28,0,6.3); ctx.fill();
    ctx.fillStyle=C.cream; ctx.fillRect(px-s*0.12, py-s*0.3, s*0.08, s*0.08); ctx.fillRect(px+s*0.04, py-s*0.3, s*0.08, s*0.08);
  } else if(a.kind==="bike"){
    ctx.strokeStyle=col; ctx.lineWidth=3;
    ctx.beginPath(); ctx.arc(px-s*0.35,py+s*0.25,s*0.28,0,6.3); ctx.stroke();
    ctx.beginPath(); ctx.arc(px+s*0.35,py+s*0.25,s*0.28,0,6.3); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(px-s*0.2,py+s*0.2); ctx.lineTo(px,py-s*0.25); ctx.lineTo(px+s*0.3,py+s*0.2); ctx.stroke();
  } else if(a.kind==="light"){
    ctx.fillStyle=C.ink; ctx.fillRect(px-3, py-s, 6, s*1.2);
    ctx.fillStyle=C.red; ctx.beginPath(); ctx.arc(px, py-s*0.7, 5,0,6.3); ctx.fill();
    ctx.fillStyle=C.yellow; ctx.beginPath(); ctx.arc(px, py-s*0.35, 4,0,6.3); ctx.fill();
  } else {
    // blocky car / truck / bus — Link Road chunky
    const bodyH = a.kind==="truck"||a.kind==="bus" ? s*0.85 : s*0.65;
    const bodyW = a.kind==="bus" ? s*1.5 : s*1.15;
    ctx.fillStyle=C.ink;
    ctx.fillRect(px-bodyW*0.5-2, py-bodyH*0.5-2, bodyW+4, bodyH+4);
    ctx.fillStyle=col;
    ctx.fillRect(px-bodyW*0.5, py-bodyH*0.5, bodyW, bodyH);
    ctx.fillStyle=C.buildDark;
    ctx.fillRect(px-bodyW*0.32, py-bodyH*0.35, bodyW*0.64, bodyH*0.35);
    ctx.fillStyle=C.cream;
    ctx.fillRect(px-bodyW*0.22, py-bodyH*0.25, 4, 4);
    ctx.fillRect(px+bodyW*0.1, py-bodyH*0.25, 4, 4);
    if(a.hazard){ ctx.strokeStyle=C.red; ctx.lineWidth=2; ctx.strokeRect(px-bodyW*0.55, py-bodyH*0.6, bodyW*1.1, bodyH*1.2); }
  }
  if(a.star && !tagged){
    ctx.fillStyle=C.amber;
    ctx.font=`${Math.max(9,s*0.45)}px Press Start 2P`;
    ctx.fillText("★", px-6, py-s*0.85 + Math.sin(tAnim*0.2)*2);
  }
  ctx.fillStyle=C.cream;
  ctx.font=`${Math.max(7,Math.floor(s*0.28))}px Press Start 2P`;
  ctx.fillText(String(a.label||a.kind).slice(0,6), px-s*0.6, py-s*0.95);
  ctx.restore();
}

function drawHero(road){
  const W=cv.width,H=cv.height, px=road.x*W, py=road.y*H, s=Math.min(W,H)*0.05*road.scale;
  ctx.fillStyle="rgba(63,242,255,0.25)"; ctx.beginPath(); ctx.arc(px,py,s*1.1,0,6.3); ctx.fill();
  ctx.fillStyle=C.ink; ctx.fillRect(px-s*0.55, py-s*0.35, s*1.1, s*0.75);
  ctx.fillStyle=C.cyan; ctx.fillRect(px-s*0.5, py-s*0.3, s, s*0.65);
  ctx.fillStyle=C.amber; ctx.fillRect(px-s*0.28, py-s*0.12, s*0.56, s*0.22);
  ctx.fillStyle=C.cream; ctx.font=`${Math.max(7,s*0.28)}px Press Start 2P`; ctx.fillText("YOU", px-s*0.45, py-s*0.5);
}

function drawWorld(actors){
  resize();
  const W=cv.width, H=cv.height;
  ctx.save();
  if(shake>0){ ctx.translate((Math.random()-.5)*shake*5,(Math.random()-.5)*shake*5); shake*=0.85; }
  ctx.imageSmoothingEnabled=false;
  drawCartoonStage(W,H);

  // sort far→near for draw order
  const mapped=actors.map(a=>{
    const r=toRoad(a.x,a.y);
    return {a,r};
  }).sort((u,v)=>u.r.y-v.r.y);

  for(const {a,r} of mapped) drawActor(a,r);

  if(hero.possess){
    const p=actors.find(x=>x.id===hero.possess);
    if(p){ const r=toRoad(p.x,p.y); hero.x=r.x; hero.y=r.y; ctx.strokeStyle=C.amber; ctx.lineWidth=3; ctx.strokeRect(r.x*W-20*r.scale, r.y*H-24*r.scale, 40*r.scale, 36*r.scale); }
    else hero.possess=null;
  }
  if(!hero.possess) drawHero({x:hero.x,y:hero.y,scale:1.15});

  for(const p of particles){
    ctx.globalAlpha=Math.max(0,p.life/40); ctx.fillStyle=p.color;
    ctx.fillRect(p.x*W, p.y*H, p.r, p.r);
    p.x+=p.vx; p.y+=p.vy; p.vy+=0.0007; p.life--;
  }
  particles=particles.filter(p=>p.life>0); ctx.globalAlpha=1;
  for(const f of floats){
    ctx.globalAlpha=f.life/40; ctx.fillStyle=f.color;
    ctx.font="9px Press Start 2P"; ctx.fillText(f.text, f.x*W, f.y*H-(40-f.life));
    f.life--;
  }
  floats=floats.filter(f=>f.life>0); ctx.globalAlpha=1;

  if(flash>0){ ctx.fillStyle=`rgba(255,211,77,${flash})`; ctx.fillRect(0,0,W,H); flash*=0.88; }
  if(power==="slow"){ ctx.fillStyle="rgba(11,135,148,0.12)"; ctx.fillRect(0,0,W,H); }
  if(power==="shield"){ ctx.strokeStyle="#58d068"; ctx.lineWidth=4; ctx.strokeRect(4,4,W-8,H-8); }

  if(win){ ctx.fillStyle="rgba(47,158,90,0.3)"; ctx.fillRect(0,0,W,H); ctx.fillStyle=C.cream; ctx.font="16px Press Start 2P"; ctx.fillText("STREET CLEARED", W*0.22, H*0.48); }
  if(fail){ ctx.fillStyle="rgba(232,72,56,0.35)"; ctx.fillRect(0,0,W,H); ctx.fillStyle=C.cream; ctx.font="16px Press Start 2P"; ctx.fillText("SPLAT — R RETRY", W*0.2, H*0.5); }
  ctx.restore();
}

function bumpMission(kind){
  const ms=world?.timeline?.missions||[];
  for(const m of ms){
    if(m.kind==="survive"||m.kind==="combo") continue;
    if(m.kind==="any"||m.kind===kind||(m.kind==="car"&&(kind==="car"||kind==="truck"||kind==="bus"))){
      const before=missionProg[m.id]||0; missionProg[m.id]=before+1;
      if(before<m.target && missionProg[m.id]>=m.target){ score+=m.reward||200; floatText(hero.x,hero.y-0.06,`+${m.reward}`,C.teal); toast(`${m.label}`,900); beep(880,.1,"triangle"); spawnBurst(hero.x,hero.y,C.teal,14); }
    }
  }
  if(maxCombo>=5){ const m=ms.find(x=>x.kind==="combo"); if(m && (missionProg.fever||0)<m.target){ missionProg.fever=m.target; score+=m.reward||500; toast("FEVER MISSION",1000); } }
  scoreEl.textContent=score; renderMissions(); checkWin();
}
function checkWin(){
  const ms=world?.timeline?.missions||[];
  const need=ms.filter(m=>m.kind!=="survive");
  const tagsDone=need.every(m=> m.kind==="combo" ? maxCombo>=m.target : (missionProg[m.id]||0)>=m.target);
  if(tagsDone && !win && !fail){
    win=true; missionProg.survive=1;
    score+=800+combo*40+starCount*60; scoreEl.textContent=score;
    const stars=score>2500?3:score>1400?2:1;
    starRate.textContent="★".repeat(stars)+"☆".repeat(3-stars)+`  ${score}`;
    missionChip.textContent="CLEARED"; missionChip.className="chip ok"; renderMissions();
    toast("STREET CLEARED",1500); beep(523,.08); setTimeout(()=>beep(659,.08),80); setTimeout(()=>beep(784,.12),160);
    spawnBurst(.5,.5,C.amber,36); try{vid.pause();}catch(e){}
  }
}
function grantPower(name){
  power=name; powerT=240; powerChip.textContent=`POWER ${name.toUpperCase()}`; powerChip.className="chip ok";
  toast(name.toUpperCase(),700); if(name==="slow"){ try{vid.playbackRate=0.55;}catch(e){} } beep(700,.08,"triangle");
}
function triggerFever(){
  if(fever<80||fail||win) return;
  fever=Math.max(0,fever-80); feverFill.style.width=fever+"%";
  grantPower(["magnet","slow","shield","double"][Math.floor(Math.random()*4)]);
  spawnBurst(hero.x,hero.y,C.red,20); flash=0.4;
}

function step(){
  tAnim++;
  const t=vid.currentTime||0;
  const dur=(world?.timeline?.duration)||vid.duration||1;
  let actors=softActors(actorsAt(t));
  syncChip.textContent=`${t.toFixed(1)}s · ${actors.length} on road`;

  if(powerT>0){ powerT--; if(powerT<=0){ if(power==="slow"){ try{vid.playbackRate=1;}catch(e){} } power=null; powerChip.textContent="POWER NONE"; powerChip.className="chip"; } }

  const accel=0.0024, friction=0.8, maxv=0.02;
  if(!hero.possess && !fail && !win){
    if(keys["arrowleft"]||keys["a"]) hero.vx-=accel;
    if(keys["arrowright"]||keys["d"]) hero.vx+=accel;
    if(keys["arrowup"]||keys["w"]) hero.vy-=accel;
    if(keys["arrowdown"]||keys["s"]) hero.vy+=accel;
    hero.vx*=friction; hero.vy*=friction;
    hero.vx=Math.max(-maxv,Math.min(maxv,hero.vx)); hero.vy=Math.max(-maxv,Math.min(maxv,hero.vy));
    hero.x=Math.max(0.08,Math.min(0.92,hero.x+hero.vx));
    hero.y=Math.max(0.34,Math.min(0.94,hero.y+hero.vy));
  }

  // magnet pulls road-space
  if(power==="magnet"){
    for(const a of actors){
      if(taggedIds.has(a.id)||!a.playable) continue;
      const r=toRoad(a.x,a.y);
      // nudge raw coords toward hero projection inverse-ish
      a.x += ((hero.x-0.5)/1.2 + 0.5 - a.x)*0.03;
      a.y += ((hero.y-0.22)/0.72 - a.y)*0.03;
    }
  }

  if(keys["p"]){
    keys["p"]=false;
    if(hero.possess){ hero.possess=null; toast("HOP OUT",500); }
    else {
      let best=null, bd=0.12;
      for(const a of actors){
        if(!a.playable||taggedIds.has(a.id)) continue;
        const r=toRoad(a.x,a.y);
        const d=Math.hypot(r.x-hero.x,r.y-hero.y);
        if(d<bd){ bd=d; best=a; }
      }
      if(best){ hero.possess=best.id; score+=40; scoreEl.textContent=score; floatText(hero.x,hero.y,"HOP",C.cyan); beep(440,.05); flash=0.2; }
    }
  }

  if((keys[" "]||keys["space"]) && !fail && !win){
    keys[" "]=keys["space"]=false;
    let hit=null, bd=power==="magnet"?0.14:0.1;
    for(const a of actors){
      if(taggedIds.has(a.id)||a.kind==="light"||a.kind==="sign"||a.kind==="prop") continue;
      const r=toRoad(a.x,a.y);
      const d=Math.hypot(r.x-hero.x,r.y-hero.y);
      if(d<bd){ bd=d; hit=a; }
    }
    if(hit){
      taggedIds.add(hit.id);
      const now=performance.now();
      combo=(now-lastTagAt<2000)?combo+1:1; lastTagAt=now; maxCombo=Math.max(maxCombo,combo);
      const mult=(power==="double"?2:1)*Math.max(1,combo);
      let pts=(hit.hazard?140:80)*mult;
      if(hit.star){ pts+=90; starCount++; starsEl.textContent=starCount; if(starCount%3===0) grantPower(["magnet","slow","shield","double"][starCount%4]); }
      score+=pts; fever=Math.min(100,fever+12+combo*2);
      scoreEl.textContent=score; comboEl.textContent=combo; feverFill.style.width=fever+"%";
      const rr=toRoad(hit.x,hit.y);
      floatText(rr.x,rr.y,`+${pts}`, combo>=4?C.red:C.amber);
      spawnBurst(rr.x,rr.y,PAL[hit.kind]||C.amber,12);
      beep(520+combo*40,.05);
      if(combo===5) toast("COMBO x5",800);
      if(combo===8){ toast("UNSTOPPABLE",800); grantPower("double"); }
      bumpMission(hit.kind);
      if(hero.possess===hit.id) hero.possess=null;
      if(fever>=100){ toast("PRESS F",900); missionChip.textContent="FEVER READY"; missionChip.className="chip hot"; }
    }
  }

  if(hurtCd>0) hurtCd--;
  if(!fail && !win && hurtCd<=0 && power!=="shield"){
    for(const a of actors){
      if(!a.hazard||taggedIds.has(a.id)) continue;
      const r=toRoad(a.x,a.y);
      if(Math.hypot(r.x-hero.x,r.y-hero.y)<0.055){
        lives--; livesEl.textContent=lives; hurtCd=50; combo=0; comboEl.textContent="0";
        fever=Math.max(0,fever-20); feverFill.style.width=fever+"%";
        shake=1; flash=0.5; spawnBurst(hero.x,hero.y,C.red,16); beep(140,.12,"sawtooth",0.04); toast("OUCH",500);
        if(lives<=0){ fail=true; missionChip.textContent="SPLAT — R"; missionChip.className="chip bad"; try{vid.pause();}catch(e){} }
        break;
      }
    }
  }

  if(!win && !fail && vid.ended && lives>0){
    const rush=world?.timeline?.missions?.find(m=>m.id==="rush");
    if(rush && (missionProg.rush||0)>=rush.target) checkWin();
    else { missionChip.textContent="TIME — FINISH TAGS / R"; missionChip.className="chip hot"; }
  }

  drawWorld(actors);
  requestAnimationFrame(step);
}
requestAnimationFrame(step);

$("searchBtn").onclick=()=>search(qEl.value.trim()||"busy intersection with cars and pedestrians").catch(e=>hitsEl.innerHTML=`<div class="hit">ERROR: ${esc(e.message)}</div>`);
$("demoBtn").onclick=()=>{ qEl.value=["pedestrians and cars at a city crosswalk","motorcycle in traffic near crosswalk","truck and cars at busy intersection"][Math.floor(Math.random()*3)]; $("searchBtn").click(); };
$("playBtn").onclick=()=>{ restartLevel(); vid.play().catch(()=>{}); toast("GO",600); beep(784,.08); };
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
