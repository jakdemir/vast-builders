#!/usr/bin/env python3
"""Wind Watch — find clips with signs of wind in the VSS archive."""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _login(force: bool = False) -> str:
    global _token
    with _token_lock:
        if _token and not force:
            return _token
        if not VSS_URL or not VSS_USERNAME or not VSS_PASSWORD:
            raise RuntimeError("VSS_URL / VSS_USERNAME / VSS_PASSWORD not set")
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
        token = _login(force=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    return r


def _normalize(hit: dict[str, Any], query_id: str, query_label: str) -> dict[str, Any]:
    source = hit.get("source") or ""
    return {
        "id": source,
        "source": source,
        "filename": hit.get("filename"),
        "original_video": hit.get("original_video"),
        "segment_number": hit.get("segment_number"),
        "camera_id": hit.get("camera_id"),
        "location": hit.get("location"),
        "capture_type": hit.get("capture_type"),
        "similarity_score": hit.get("similarity_score"),
        "segment_start_sec": hit.get("segment_start_sec"),
        "segment_end_sec": hit.get("segment_end_sec"),
        "reasoning_content": hit.get("reasoning_content") or "",
        "object_classes": hit.get("object_classes") or "",
        "query_id": query_id,
        "query_label": query_label,
        # Relative path so the UI can prefix /app when behind Ingress rewrite.
        "stream_path": f"api/stream?source={quote(source, safe='')}",
    }


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "windwatch"})


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

    out_headers = {
        "Content-Type": ct if (ct := upstream.headers.get("Content-Type", "")).startswith("video/") else "video/mp4",
        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
    }
    if "Content-Length" in upstream.headers:
        out_headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        out_headers["Content-Range"] = upstream.headers["Content-Range"]
    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=out_headers,
        direct_passthrough=True,
    )


WIND_CUES = [
    {"id": "all", "label": "Any wind",
     "query": "wind or windy conditions: swaying trees, blowing debris, flapping flags, clothing whipped, people leaning into wind"},
    {"id": "trees", "label": "Trees swaying",
     "query": "branches swaying noticeably in the wind, leaves and foliage moving due to wind"},
    {"id": "debris", "label": "Blowing debris",
     "query": "leaves, dust, paper or plastic bags blowing and tumbling across the ground in the wind"},
    {"id": "flags", "label": "Flags / fabric",
     "query": "flags, banners, awnings, tarps or netting flapping and rippling in the wind"},
    {"id": "clothing", "label": "Clothing / hair",
     "query": "clothing, hair or hats whipped by wind, people holding down hats or leaning into the wind"},
    {"id": "umbrella", "label": "Umbrellas",
     "query": "umbrella inverted or struggling to stay open in strong wind"},
    {"id": "sideways", "label": "Sideways rain / smoke",
     "query": "rain, mist, smoke or steam blowing sideways in the wind"},
    {"id": "fixtures", "label": "Signs / cones rocking",
     "query": "signs, traffic cones or outdoor furniture rocking or tipping over in the wind"},
]
WIND_TOP_K = 12
WIND_MIN_SIM = 0.20
# The embedder rejects queries containing newlines.
WIND_MAX_QUERY = 400
WIND_SYSTEM_PROMPT = (
    "You assess wind in video clips using only their captions. Treat a clip as showing wind only if "
    "its caption explicitly describes wind-driven motion (swaying, flapping, blowing, whipped, gusts). "
    "Trees, flags or rain on their own are not wind evidence. For each windy clip give the moment, "
    "what is moving, the stated strength and direction. If no caption describes wind, say so plainly."
)
WIND_TIME_FILTERS = ("all", "24h", "7d")
# Indoor warehouse footage has no wind; drop it from every search and summary.
WIND_EXCLUDE = re.compile(r"warehouse", re.I)
WIND_SWEEP_TOP_K = 20
_capture_types: list[str] | None = None

# Kept in sync with wind/extract_wind.py so the app and the batch table agree.
WIND_MOTION = re.compile(
    r"\b(sway\w*|rustl\w*|flap\w*|billow\w*|flutter\w*|whip\w*|thrash\w*|"
    r"ripple\w*|wind[- ]?blown|windy|gust\w*|breez\w*|"
    r"(in|by|from|due to|because of) (the )?wind|wind (is|was|appears|causes|caus\w*|blow\w*|push\w*))\b",
    re.I,
)
WIND_NEGATION = re.compile(r"\b(no|not|without|none|absent|lack\w*|neither|nor)\b", re.I)
WIND_CALM = re.compile(
    r"\b(no (clear |visible |apparent |obvious )?(evidence of |signs? of |indication of )?wind"
    r"|no wind|wind(less)?\b.{0,20}\b(absent|not (present|visible|evident))"
    r"|no (visible )?movement of (the )?(foliage|trees?|leaves|branches|vegetation)"
    r"|(foliage|trees?|leaves|branches|vegetation) (is|are|remains?|appears?) (still|motionless|stationary|unmoving)"
    r"|(air|weather|conditions) (is|are|appears?|remains?) (calm|still|windless))\b",
    re.I,
)
WIND_HEDGE = re.compile(r"\b(likely|possibly|perhaps|may|might|could|or (some )?other|appears to be caused)\b", re.I)
WIND_STRENGTH = [
    ("strong", re.compile(r"\b(strong(ly)?|heav(y|ily)|high|gale|intense(ly)?|violent(ly)?|powerful(ly)?|forceful(ly)?|sever(e|ely)|vigorous(ly)?)\b", re.I)),
    ("moderate", re.compile(r"\b(moderate(ly)?|noticeabl\w*|stead(y|ily)|continuous(ly)?)\b", re.I)),
    ("light", re.compile(r"\b(light(ly)?|gentl(e|y)|slight\w*|mild(ly)?|subtl(e|y)|faint(ly)?|breez\w*)\b", re.I)),
]
WIND_CUE_RX = {
    "trees": re.compile(r"\b(tree|branch|foliage|lea(f|ves)|bush|shrub|plant|vegetation|grass)", re.I),
    "debris": re.compile(r"\b(debris|dust|sand|pollen|paper|plastic bag|bags?|trash|litter|cardboard)\b", re.I),
    "flags": re.compile(r"\b(flag|banner|pennant|awning|canop|tarp|netting|tent|poster|decoration)", re.I),
    "clothing": re.compile(r"\b(cloth|jacket|coat|skirt|scarf|hair|hat|hood|cap)\b", re.I),
    "umbrella": re.compile(r"\bumbrella", re.I),
    "sideways": re.compile(r"\b(smoke|steam|exhaust|mist|rain|spray)\b", re.I),
    "fixtures": re.compile(r"\b(sign|cone|chair|furniture|menu|vendor|rack)", re.I),
}
WIND_DIRECTION = re.compile(
    r"\b((from|toward|towards|to) the (left|right|north|south|east|west)"
    r"|(left|right)[- ]to[- ](left|right)|(north|south|east|west)ward)\b",
    re.I,
)
WIND_PRESENT_RANK = {"yes": 0, "possible": 1, "unclear": 2, "no": 3}
WIND_STRENGTH_RANK = {"strong": 0, "moderate": 1, "light": 2, "unknown": 3, "none": 4}


def _wind_classify(caption: str) -> dict[str, Any]:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", caption or "") if s.strip()]
    positive = [s for s in sentences if WIND_MOTION.search(s) and not WIND_NEGATION.search(s)]
    if positive:
        present = "possible" if all(WIND_HEDGE.search(s) for s in positive) else "yes"
    elif any(WIND_CALM.search(s) for s in sentences):
        present = "no"
    else:
        present = "unclear"
    text = " ".join(positive)
    strength = "none" if present == "no" else "unknown"
    if positive:
        strength = next((label for label, rx in WIND_STRENGTH if rx.search(text)), "unknown")
    direction = WIND_DIRECTION.search(text) if positive else None
    return {
        "wind_present": present,
        "wind_strength": strength,
        "wind_cues": [name for name, rx in WIND_CUE_RX.items() if positive and rx.search(text)],
        "wind_direction": direction.group(0).lower() if direction else "",
        "wind_evidence": positive,
        "wind_mentioned": present in ("yes", "possible"),
    }


def _excluded(hit: dict[str, Any]) -> bool:
    return any(
        WIND_EXCLUDE.search(str(hit.get(f) or "")) for f in ("capture_type", "location", "camera_id")
    )


def _allowed_capture_types() -> list[str]:
    global _capture_types
    if _capture_types is None:
        r = _vss("GET", "/api/v1/metadata/values", params={"field": "capture_type"}, timeout=30)
        r.raise_for_status()
        _capture_types = [v for v in r.json().get("values") or [] if not WIND_EXCLUDE.search(v)]
    return _capture_types


def _wind_search(
    query: str, time_filter: str, synthesize: bool, capture_type: str | None = None, top_k: int = WIND_TOP_K
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "top_k": top_k,
        "llm_top_n": top_k if synthesize else 1,
        "min_similarity": WIND_MIN_SIM,
        "include_public": True,
        "time_filter": time_filter,
    }
    if capture_type:
        payload["metadata_filters"] = {"capture_type": capture_type}
    if synthesize:
        payload["system_prompt"] = WIND_SYSTEM_PROMPT
    r = _vss("POST", "/api/v1/search", json=payload)
    r.raise_for_status()
    return r.json()


def _wind_items(data: dict[str, Any], cue_id: str, label: str) -> list[dict[str, Any]]:
    items = []
    for hit in data.get("results") or []:
        if not hit.get("source") or _excluded(hit):
            continue
        item = _normalize(hit, cue_id, label)
        item.update(_wind_classify(item["reasoning_content"]))
        item["reingested"] = "/reingest/" in item["source"]
        item["matched_cues"] = [label]
        items.append(item)
    return items


def _wind_sort(items: list[dict[str, Any]]) -> None:
    items.sort(key=lambda i: (
        WIND_PRESENT_RANK.get(i["wind_present"], 9),
        WIND_STRENGTH_RANK.get(i["wind_strength"], 9),
        -(i.get("similarity_score") or 0),
    ))


def _wind_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    windy = [i for i in items if i["wind_mentioned"]]
    cameras: dict[str, dict[str, Any]] = {}
    for i in windy:
        cam = cameras.setdefault(i.get("camera_id") or "unknown", {
            "camera_id": i.get("camera_id") or "unknown", "location": i.get("location") or "",
            "clips": 0, "strong": 0,
        })
        cam["clips"] += 1
        cam["strong"] += i["wind_strength"] == "strong"
    return {
        "present": dict(Counter(i["wind_present"] for i in items)),
        "strength": dict(Counter(i["wind_strength"] for i in windy)),
        "cameras": sorted(cameras.values(), key=lambda c: (-c["strong"], -c["clips"]))[:8],
    }


def _synthesis(data: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    synth = data.get("llm_synthesis") or {}
    text = re.sub(r"<think>.*?</think>", "", synth.get("response") or "", flags=re.S).strip()
    # The LLM numbers clips ("Clip N") in segments_analyzed order.
    clips = []
    for entry in synth.get("segments_analyzed") or []:
        m = re.match(r"(.*?)\s*\(best moment ([^)]*)\)$", entry)
        clips.append({"file": m.group(1) if m else entry, "moment": m.group(2) if m else ""})
    return text, clips


def _time_filter(body: dict[str, Any]) -> str:
    tf = body.get("time_filter") or "all"
    return tf if tf in WIND_TIME_FILTERS else "all"


@app.get("/api/wind/cues")
def api_wind_cues():
    return jsonify({
        "cues": [{"id": c["id"], "label": c["label"]} for c in WIND_CUES],
        "time_filters": list(WIND_TIME_FILTERS),
    })


@app.post("/api/wind")
def api_wind():
    body = request.get_json(force=True, silent=True) or {}
    cue = next((c for c in WIND_CUES if c["id"] == body.get("cue")), None)
    custom = " ".join(str(body.get("query") or "").split())[:WIND_MAX_QUERY]
    if cue is None and not custom:
        return jsonify({"error": "cue or query required"}), 400
    label = cue["label"] if cue else "Custom"
    query = cue["query"] if cue else custom
    tf = _time_filter(body)
    try:
        types = _allowed_capture_types()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Search each non-warehouse capture type separately so the LLM summary never sees warehouse clips.
    by_source: dict[str, dict[str, Any]] = {}
    sections = []
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, len(types))) as pool:
        futures = {pool.submit(_wind_search, query, tf, True, t): t for t in types}
        for fut in as_completed(futures):
            ctype = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                errors.append(f"{ctype}: {e}")
                continue
            for item in _wind_items(data, cue["id"] if cue else "custom", label):
                by_source.setdefault(item["source"], item)
            text, clips = _synthesis(data)
            if text:
                sections.append({"title": ctype, "text": text, "clips": clips})
    if errors and not by_source:
        return jsonify({"error": "; ".join(errors)}), 500
    items = list(by_source.values())
    _wind_sort(items)
    sections.sort(key=lambda s: types.index(s["title"]))
    return jsonify({
        "query": query,
        "count": len(items),
        "wind_count": sum(1 for i in items if i["wind_mentioned"]),
        "syntheses": sections,
        "errors": errors,
        "summary": _wind_summary(items),
        "results": items,
    })


@app.post("/api/wind/sweep")
def api_wind_sweep():
    body = request.get_json(force=True, silent=True) or {}
    tf = _time_filter(body)
    cues = [c for c in WIND_CUES if c["id"] != "all"]
    by_source: dict[str, dict[str, Any]] = {}
    errors = []
    with ThreadPoolExecutor(max_workers=len(cues)) as pool:
        futures = {pool.submit(_wind_search, c["query"], tf, False, None, WIND_SWEEP_TOP_K): c for c in cues}
        for fut in as_completed(futures):
            cue = futures[fut]
            try:
                items = _wind_items(fut.result(), cue["id"], cue["label"])
            except Exception as e:
                errors.append(f"{cue['label']}: {e}")
                continue
            for item in items:
                prev = by_source.get(item["source"])
                if prev is None:
                    by_source[item["source"]] = item
                    continue
                prev["matched_cues"].append(cue["label"])
                if (item["similarity_score"] or 0) > (prev["similarity_score"] or 0):
                    prev["similarity_score"] = item["similarity_score"]
    if errors and not by_source:
        return jsonify({"error": "; ".join(errors)}), 500
    items = list(by_source.values())
    _wind_sort(items)
    return jsonify({
        "count": len(items),
        "wind_count": sum(1 for i in items if i["wind_mentioned"]),
        "summary": _wind_summary(items),
        "errors": errors,
        "results": items,
    })


WIND_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Wind Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
:root {
  --bg0: #10161a; --bg1: #172026; --ink: #e4ebe8; --muted: #8fa09b;
  --teal: #4fc3b0; --teal-dim: #2a6e64; --line: #26343a; --warn: #c9a24a;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: "IBM Plex Sans", sans-serif; color: var(--ink);
  background:
    radial-gradient(ellipse 80% 50% at 10% -10%, #183330 0%, transparent 55%),
    radial-gradient(ellipse 60% 40% at 100% 0%, #1b2633 0%, transparent 50%),
    linear-gradient(180deg, var(--bg0), #0a0f12 100%);
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 20px 48px; }
.top { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
.brand { font-family: "Bebas Neue", sans-serif; font-size: clamp(3rem, 8vw, 5.5rem);
  letter-spacing: 0.04em; line-height: 0.9; color: var(--teal); margin: 0 0 8px;
  text-shadow: 0 0 40px rgba(79,195,176,0.25); }
.top a { color: var(--muted); margin-left: auto; font-size: 0.9rem; }
.tagline { color: var(--muted); max-width: 40rem; margin: 0 0 22px; font-size: 1.05rem; }
.cues { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
button { font-family: inherit; font-weight: 600; cursor: pointer; border: none;
  border-radius: 2px; padding: 10px 16px; font-size: 0.9rem; }
.cue { background: transparent; color: var(--ink); border: 1px solid var(--line); }
.cue:hover, .cue.on { border-color: var(--teal); color: var(--teal); }
button:disabled { opacity: 0.5; cursor: wait; }
.search { display: flex; gap: 8px; margin-bottom: 18px; }
.search input { flex: 1; font: inherit; padding: 10px 12px; background: var(--bg1);
  color: var(--ink); border: 1px solid var(--line); border-radius: 2px; }
.primary { background: var(--teal); color: #04140f; }
.meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 14px; }
.synth { background: var(--bg1); border-left: 3px solid var(--teal); padding: 12px 14px;
  font-size: 0.88rem; line-height: 1.45; white-space: pre-wrap; margin-bottom: 18px;
  max-height: 22rem; overflow: auto; }
.layout { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
.queue { border-top: 2px solid var(--teal-dim); }
.item { display: grid; grid-template-columns: auto 1fr; gap: 10px 14px; padding: 14px 4px;
  border-bottom: 1px solid var(--line); cursor: pointer; align-items: start; }
.item:hover, .item.active { background: rgba(79,195,176,0.06); }
.score { font-family: "Bebas Neue", sans-serif; font-size: 1.4rem; color: var(--teal);
  min-width: 3rem; text-align: right; padding-top: 2px; }
.item h3 { margin: 0 0 4px; font-size: 0.95rem; font-weight: 600; }
.item p { margin: 0; color: var(--muted); font-size: 0.82rem; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.badge { display: inline-block; font-size: 0.7rem; letter-spacing: 0.04em; text-transform: uppercase;
  padding: 2px 7px; border: 1px solid var(--line); color: var(--muted); margin-right: 6px; }
.badge.wind { border-color: var(--teal); color: var(--teal); }
.badge.re { border-color: var(--warn); color: var(--warn); }
.panel { background: var(--bg1); border: 1px solid var(--line); padding: 14px; position: sticky; top: 12px; }
video { width: 100%; background: #000; aspect-ratio: 16/9; display: block; }
.reason { margin: 12px 0 0; font-size: 0.88rem; line-height: 1.45; max-height: 12rem; overflow: auto; }
.reason mark { background: rgba(79,195,176,0.22); color: var(--ink); padding: 0 2px; }
.facts { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.now { font-size: 0.85rem; margin-bottom: 8px; word-break: break-all; }
.now b { color: var(--teal); font-weight: 600; }
.now span { color: var(--muted); }
.synth h5 { margin: 0; font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--teal); }
.synth a.clipref { color: var(--teal); font-weight: 600; text-decoration: none; border-bottom: 1px dotted var(--teal); }
.synth a.clipref:hover { border-bottom-style: solid; }
.empty { color: var(--muted); padding: 24px 0; }
.controls { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
.controls select { font: inherit; background: var(--bg1); color: var(--ink); border: 1px solid var(--line);
  padding: 8px 10px; border-radius: 2px; }
.controls label { color: var(--muted); font-size: 0.9rem; display: flex; gap: 6px; align-items: center; }
.sweep { background: transparent; color: var(--teal); border: 1px solid var(--teal); }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: var(--bg1); border: 1px solid var(--line); padding: 12px 14px; }
.card h4 { margin: 0 0 8px; font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.stat { display: flex; justify-content: space-between; font-size: 0.86rem; padding: 2px 0; }
.stat b { color: var(--teal); font-weight: 600; }
.bars { display: flex; height: 8px; margin-top: 6px; background: var(--line); }
.bars span { display: block; height: 100%; }
.badge.yes { border-color: var(--teal); color: var(--teal); }
.badge.possible { border-color: var(--warn); color: var(--warn); }
.badge.strong { background: rgba(214,92,72,0.15); border-color: #d65c48; color: #e38a7a; }
.badge.moderate { border-color: var(--warn); color: var(--warn); }
.badge.light { border-color: var(--teal-dim); color: var(--teal); }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1 class="brand">Wind Watch</h1>
    <a id="backLink" href="./">&larr; App home</a>
  </div>
  <p class="tagline">Find clips with signs of wind. Sweep the whole archive, pick a single cue, or describe what to look for. A clip only counts as windy when its caption describes wind-driven motion. Indoor warehouse footage is excluded.</p>
  <div class="controls">
    <button class="sweep" id="sweepBtn" type="button">Sweep all cues</button>
    <select id="timeFilter" aria-label="Time range">
      <option value="all">All time</option>
      <option value="7d">Last 7 days</option>
      <option value="24h">Last 24 hours</option>
    </select>
    <label><input type="checkbox" id="windOnly"/> Wind only</label>
  </div>
  <div class="cues" id="cues"></div>
  <form class="search" id="searchForm">
    <input id="q" placeholder="e.g. palm trees bending in strong wind" maxlength="400"/>
    <button class="primary" id="goBtn" type="submit">Search</button>
  </form>
  <div class="meta" id="statusLine">Sweep the archive or pick a cue to start.</div>
  <div class="summary" id="summary" hidden></div>
  <div class="synth" id="synth" hidden></div>
  <div class="layout">
    <div class="queue" id="queue"></div>
    <div class="panel">
      <div class="now" id="now"></div>
      <video id="player" controls playsinline></video>
      <div class="facts" id="facts"></div>
      <div class="reason" id="reason">Select a clip to watch it and read its caption.</div>
    </div>
  </div>
</div>
<script>
const BASE = (window.location.pathname.match(/^\/app(?=\/|$)/) || [""])[0];
const api = (path) => `${BASE}/${path.replace(/^\//, "")}`;
document.getElementById("backLink").href = `${BASE}/`;

const cuesEl = document.getElementById("cues");
const queueEl = document.getElementById("queue");
const player = document.getElementById("player");
const reason = document.getElementById("reason");
const statusLine = document.getElementById("statusLine");
const synthEl = document.getElementById("synth");
const nowEl = document.getElementById("now");
const summaryEl = document.getElementById("summary");
const factsEl = document.getElementById("facts");
const qInput = document.getElementById("q");
const timeFilter = document.getElementById("timeFilter");
const windOnly = document.getElementById("windOnly");
let allResults = [];
let results = [];
let selected = null;
let busy = false;
let lastRun = null;
const STRENGTH_COLORS = {strong: "#d65c48", moderate: "#c9a24a", light: "#4fc3b0", unknown: "#5b6b70"};

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function baseName(path) {
  return String(path || "").split("/").pop();
}

function videoName(file) {
  return baseName(file).replace(/\.mp4$/i, "").replace(/^\d{8}_\d{6}_/, "");
}

function fmtTime(sec) {
  const s = Math.round(sec || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function clipLabel(r) {
  return `${videoName(r.original_video || r.filename)} · segment ${r.segment_number ?? "?"} (${fmtTime(r.segment_start_sec)}–${fmtTime(r.segment_end_sec)})`;
}

function renderSynthesis(text, clips) {
  const ref = /Clip\s+(\d+)(?:\s*,?\s*\(?\s*Segment\s+(\d+)\s*\)?)?(?:\s*\((\d+:\d+)\s*[–-]\s*(\d+:\d+)\))?/gi;
  let html = esc(text).replace(ref, (m, n, seg, t0, t1) => {
    const clip = clips[+n - 1];
    if (!clip) return m;
    const same = allResults.filter(r => baseName(r.original_video) === clip.file);
    const hit = same.find(r => seg && +r.segment_number === +seg) || (!seg && same[0]);
    let label = videoName(clip.file);
    if (hit) label = clipLabel(hit);
    else if (seg) label += ` · segment ${seg}${t0 ? ` (${t0}–${t1})` : ""}`;
    else if (clip.moment) label += ` (${clip.moment})`;
    if (!hit) return `<span title="Not in the result list">${esc(label)}</span>`;
    return `<a href="#" class="clipref" data-i="${allResults.indexOf(hit)}">${esc(label)}</a>`;
  });
  return html.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

synthEl.addEventListener("click", (e) => {
  const a = e.target.closest("a.clipref");
  if (!a) return;
  e.preventDefault();
  const r = allResults[+a.dataset.i];
  if (!r) return;
  if (windOnly.checked && !r.wind_mentioned) { windOnly.checked = false; }
  select(r);
  player.scrollIntoView({behavior: "smooth", block: "center"});
});

function windBadges(r) {
  const out = [];
  if (r.wind_present === "yes") out.push('<span class="badge yes">wind</span>');
  else if (r.wind_present === "possible") out.push('<span class="badge possible">possible wind</span>');
  else if (r.wind_present === "no") out.push('<span class="badge">calm</span>');
  else out.push('<span class="badge">no wind mention</span>');
  if (r.wind_mentioned && r.wind_strength !== "unknown") out.push(`<span class="badge ${esc(r.wind_strength)}">${esc(r.wind_strength)}</span>`);
  if (r.reingested) out.push('<span class="badge re">re-ingested</span>');
  return out.join("");
}

function renderQueue() {
  results = windOnly.checked ? allResults.filter(r => r.wind_mentioned) : allResults;
  if (!results.length) {
    queueEl.innerHTML = `<p class="empty">${allResults.length ? "No clips with wind in the caption. Untick Wind only to see all matches." : "No matching clips."}</p>`;
    return;
  }
  queueEl.innerHTML = results.map((r, i) => `
    <div class="item ${selected && selected.id === r.id ? "active" : ""}" data-i="${i}">
      <div class="score">${(r.similarity_score || 0).toFixed(2)}</div>
      <div>
        <h3>${windBadges(r)}</h3>
        <p><strong>${esc(clipLabel(r))}</strong><br/>${esc(r.camera_id)} · ${esc(r.location)}${(r.matched_cues || []).length > 1 ? ` · ${r.matched_cues.length} cues` : ""}<br/>${esc((r.wind_evidence || [])[0] || r.reasoning_content)}</p>
      </div>
    </div>`).join("");
  queueEl.querySelectorAll(".item").forEach(el => {
    el.addEventListener("click", () => select(results[+el.dataset.i]));
  });
}

function highlight(caption, evidence) {
  let html = esc(caption || "(no caption)");
  for (const s of evidence || []) {
    const e = esc(s);
    if (e && html.includes(e)) html = html.replace(e, `<mark>${e}</mark>`);
  }
  return html;
}

function select(r) {
  selected = r;
  player.src = api(r.stream_path);
  player.play().catch(() => {});
  nowEl.innerHTML = `<b>${esc(videoName(r.original_video || r.filename))}</b><br/><span>Segment ${esc(r.segment_number ?? "?")} · ${fmtTime(r.segment_start_sec)}–${fmtTime(r.segment_end_sec)} · ${esc(r.camera_id)}${r.location ? " · " + esc(r.location) : ""}</span>`;
  const facts = [];
  if (r.wind_mentioned) facts.push(`<span class="badge ${esc(r.wind_strength)}">strength: ${esc(r.wind_strength)}</span>`);
  if (r.wind_direction) facts.push(`<span class="badge">direction: ${esc(r.wind_direction)}</span>`);
  for (const c of r.wind_cues || []) facts.push(`<span class="badge wind">${esc(c)}</span>`);
  for (const c of r.matched_cues || []) facts.push(`<span class="badge">matched: ${esc(c)}</span>`);
  factsEl.innerHTML = facts.join("");
  reason.innerHTML = highlight(r.reasoning_content, r.wind_evidence);
  renderQueue();
}

function renderSummary(s, total) {
  if (!s || !total) { summaryEl.hidden = true; return; }
  const p = s.present || {};
  const windy = (p.yes || 0) + (p.possible || 0);
  const str = s.strength || {};
  const bars = ["strong", "moderate", "light", "unknown"].filter(k => str[k])
    .map(k => `<span style="width:${100 * str[k] / windy}%;background:${STRENGTH_COLORS[k]}" title="${k}: ${str[k]}"></span>`).join("");
  const cams = (s.cameras || []).map(c =>
    `<div class="stat"><span>${esc(c.camera_id)}${c.location ? ` <small style="color:var(--muted)">${esc(c.location)}</small>` : ""}</span><b>${c.clips}${c.strong ? ` · ${c.strong} strong` : ""}</b></div>`
  ).join("") || '<div class="stat"><span style="color:var(--muted)">No windy clips</span></div>';
  summaryEl.innerHTML = `
    <div class="card"><h4>Wind in captions</h4>
      <div class="stat"><span>Confirmed</span><b>${p.yes || 0}</b></div>
      <div class="stat"><span>Possible</span><b>${p.possible || 0}</b></div>
      <div class="stat"><span>Calm / no mention</span><b>${(p.no || 0) + (p.unclear || 0)}</b></div>
    </div>
    <div class="card"><h4>Strength (windy clips)</h4>
      ${["strong", "moderate", "light", "unknown"].map(k => `<div class="stat"><span>${k}</span><b>${str[k] || 0}</b></div>`).join("")}
      ${windy ? `<div class="bars">${bars}</div>` : ""}
    </div>
    <div class="card"><h4>Windiest cameras</h4>${cams}</div>`;
  summaryEl.hidden = false;
}

async function run(path, payload, label) {
  if (busy) return;
  busy = true;
  lastRun = {path, payload, label};
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  statusLine.textContent = `Searching: ${label}…`;
  try {
    const res = await fetch(api(path), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...payload, time_filter: timeFilter.value}),
    });
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) throw new Error(`non-JSON response (${res.status})`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "search failed");
    allResults = data.results || [];
    selected = null;
    player.removeAttribute("src");
    factsEl.innerHTML = "";
    nowEl.innerHTML = "";
    reason.textContent = "Select a clip to watch it and read its caption.";
    const partial = (data.errors || []).length ? ` · ${data.errors.length} cue(s) failed` : "";
    statusLine.textContent = `${label}: ${data.count} clips · ${data.wind_count} with wind in the caption${partial}`;
    const sections = data.syntheses || [];
    synthEl.hidden = !sections.length;
    synthEl.innerHTML = sections.map(s =>
      `<h5>${esc(s.title)}</h5>${renderSynthesis(s.text, s.clips || [])}`
    ).join("\n\n");
    renderSummary(data.summary, data.count);
    renderQueue();
    if (results[0]) select(results[0]);
  } catch (e) {
    statusLine.textContent = "Search failed: " + e.message;
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach(b => b.disabled = false);
  }
}

async function loadCues() {
  const res = await fetch(api("api/wind/cues"));
  const data = await res.json();
  cuesEl.innerHTML = data.cues.map(c => `<button class="cue" type="button" data-id="${esc(c.id)}">${esc(c.label)}</button>`).join("");
  cuesEl.querySelectorAll(".cue").forEach(btn => {
    btn.addEventListener("click", () => {
      cuesEl.querySelectorAll(".cue").forEach(b => b.classList.toggle("on", b === btn));
      run("api/wind", {cue: btn.dataset.id}, btn.textContent);
    });
  });
}

document.getElementById("searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = qInput.value.trim();
  if (!q) return;
  cuesEl.querySelectorAll(".cue").forEach(b => b.classList.remove("on"));
  run("api/wind", {query: q}, "Custom");
});

document.getElementById("sweepBtn").addEventListener("click", () => {
  cuesEl.querySelectorAll(".cue").forEach(b => b.classList.remove("on"));
  run("api/wind/sweep", {}, "Sweep");
});

windOnly.addEventListener("change", () => {
  renderQueue();
  if (selected && !results.includes(selected) && results[0]) select(results[0]);
});

timeFilter.addEventListener("change", () => {
  if (lastRun) run(lastRun.path, lastRun.payload, lastRun.label);
});

loadCues().catch(() => { statusLine.textContent = "Could not load cues."; });
</script>
</body>
</html>
"""


@app.get("/")
@app.get("/wind")
def wind_page():
    return Response(WIND_PAGE, mimetype="text/html")


def main():
    try:
        _login()
        print("windwatch: VSS login ok", flush=True)
    except Exception as e:
        print(f"windwatch: VSS login deferred ({e})", flush=True)
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

        Standalone(
            app,
            {
                "bind": f"0.0.0.0:{PORT}",
                "workers": 1,
                "threads": 4,
                "timeout": 120,
            },
        ).run()
    except Exception:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
