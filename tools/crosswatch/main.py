#!/usr/bin/env python3
"""CrossWatch — curated illegal-walk cases with multiple re-ingested clips each."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
CASES_PATH = Path(__file__).with_name("cases.json")

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


def _load_cases() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _public_cases() -> list[dict[str, Any]]:
    data = _load_cases()
    out = []
    for c in data.get("cases") or []:
        videos = []
        for v in c.get("videos") or []:
            source = v.get("source") or ""
            videos.append(
                {
                    "original_video": v.get("original_video"),
                    "camera_id": v.get("camera_id"),
                    "location": v.get("location"),
                    "source": source,
                    "reasoning_content": v.get("reasoning_content") or "",
                    "segment_start_sec": v.get("segment_start_sec"),
                    "segment_end_sec": v.get("segment_end_sec"),
                    "stream_path": f"api/stream?source={quote(source, safe='')}" if source else None,
                }
            )
        out.append(
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "video_count": len(videos),
                "videos": videos,
            }
        )
    return out


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "crosswatch", "mode": "curated_multi"})


@app.get("/api/cases")
def api_cases():
    try:
        cases = _public_cases()
        total = sum(c["video_count"] for c in cases)
        return jsonify({"count": len(cases), "video_total": total, "results": cases})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
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


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CrossWatch</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
:root {
  --bg0: #12141a; --bg1: #1a1e28; --ink: #e8e4dc; --muted: #9a958a;
  --amber: #e8a317; --amber-dim: #8a6210; --line: #2c3240;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: "IBM Plex Sans", sans-serif; color: var(--ink);
  background:
    radial-gradient(ellipse 80% 50% at 10% -10%, #2a2418 0%, transparent 55%),
    linear-gradient(180deg, var(--bg0), #0c0e12 100%);
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 20px 48px; }
.brand {
  font-family: "Bebas Neue", sans-serif;
  font-size: clamp(3rem, 8vw, 5rem); letter-spacing: 0.04em; line-height: 0.9;
  color: var(--amber); margin: 0 0 8px;
}
.tagline { color: var(--muted); max-width: 40rem; margin: 0 0 20px; }
.meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 16px; }
.layout { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 18px; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
.case { border-top: 2px solid var(--amber-dim); margin-bottom: 8px; }
.case-head {
  font-family: "Bebas Neue", sans-serif; font-size: 1.45rem; letter-spacing: 0.03em;
  color: var(--amber); padding: 12px 4px 6px; margin: 0;
}
.case-sub { color: var(--muted); font-size: 0.8rem; padding: 0 4px 8px; }
.item {
  padding: 10px 8px; border-bottom: 1px solid var(--line); cursor: pointer;
}
.item:hover, .item.active { background: rgba(232,163,23,0.06); }
.item h4 { margin: 0 0 4px; font-size: 0.92rem; font-weight: 600; }
.item p { margin: 0; color: var(--muted); font-size: 0.8rem; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.panel { background: var(--bg1); border: 1px solid var(--line); padding: 14px; position: sticky; top: 12px; }
video { width: 100%; background: #000; aspect-ratio: 16/9; display: block; }
.reason { margin-top: 12px; font-size: 0.9rem; line-height: 1.45; max-height: 16rem; overflow: auto; }
.empty { color: var(--muted); padding: 20px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1 class="brand">CrossWatch</h1>
  <p class="tagline">Five illegal-walk case types — multiple re-ingested street clips and reasoner captions for each.</p>
  <div class="meta" id="statusLine">Loading…</div>
  <div class="layout">
    <div id="queue"></div>
    <div class="panel">
      <video id="player" controls playsinline></video>
      <div class="reason" id="reason">Select a clip.</div>
    </div>
  </div>
</div>
<script>
// Prefer /app/crosswatch; also accept legacy /crosswatch.
const BASE = (window.location.pathname.match(/^\/app\/crosswatch(?=\/|$)/) || window.location.pathname.match(/^\/crosswatch(?=\/|$)/) || [""])[0];
const api = (path) => `${BASE}/${path.replace(/^\//, "")}`;
const queueEl = document.getElementById("queue");
const player = document.getElementById("player");
const reason = document.getElementById("reason");
const statusLine = document.getElementById("statusLine");
let cases = [];
let selectedKey = null;

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function keyOf(ci, vi) { return ci + ":" + vi; }

function render() {
  queueEl.innerHTML = cases.map((c, ci) => `
    <section class="case">
      <h2 class="case-head">${String(ci+1).padStart(2,"0")} · ${esc(c.title)}</h2>
      <div class="case-sub">${c.video_count || (c.videos||[]).length} clips</div>
      ${(c.videos || []).map((v, vi) => `
        <div class="item ${selectedKey === keyOf(ci,vi) ? "active" : ""}" data-ci="${ci}" data-vi="${vi}">
          <h4>${esc(v.camera_id)} · ${esc(v.location)}</h4>
          <p>${esc(v.reasoning_content)}</p>
        </div>`).join("")}
    </section>`).join("") || '<p class="empty">No cases loaded.</p>';
  queueEl.querySelectorAll(".item").forEach(el => {
    el.addEventListener("click", () => select(+el.dataset.ci, +el.dataset.vi));
  });
}

function select(ci, vi) {
  const c = cases[ci];
  const v = c.videos[vi];
  selectedKey = keyOf(ci, vi);
  if (v.stream_path) {
    player.src = api(v.stream_path);
    player.play().catch(() => {});
  }
  reason.innerHTML = `<strong>${esc(c.title)}</strong><br/>${esc(v.camera_id)} · ${esc(v.location)}<br/><br/>${esc(v.reasoning_content)}`;
  render();
}

async function load() {
  try {
    const res = await fetch(api("api/cases"));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "load failed");
    cases = data.results || [];
    statusLine.textContent = `${data.count} cases · ${data.video_total} clips`;
    render();
    if (cases[0] && cases[0].videos && cases[0].videos[0]) select(0, 0);
  } catch (e) {
    statusLine.textContent = "Load failed: " + e.message;
  }
}
load();
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
        print("crosswatch: VSS login ok", flush=True)
    except Exception as e:
        print(f"crosswatch: VSS login deferred ({e})", flush=True)
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
            {"bind": f"0.0.0.0:{PORT}", "workers": 1, "threads": 4, "timeout": 120},
        ).run()
    except Exception:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
