#!/usr/bin/env python3
"""CrossWatch — curated illegal-walk cases with brief, YOLO overlay, find-more."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from feat_brief import enrich_case
from feat_findmore import init_findmore
from feat_quality import clean_caption
from feat_yolo import init_yolo, yolo_bp

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
CASES_PATH = Path(__file__).with_name("cases.json")
HERE = Path(__file__).resolve().parent

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


def _is_curated_keep(v: dict[str, Any]) -> bool:
    """Curated verified / agent-backed clips are never dropped on load."""
    if v.get("verified") is True:
        return True
    support = str(v.get("caption_support") or "")
    return support.startswith("agent")


def _public_cases() -> list[dict[str, Any]]:
    data = _load_cases()
    out = []
    for c in data.get("cases") or []:
        videos = []
        for v in c.get("videos") or []:
            source = v.get("source") or ""
            caption = clean_caption(v.get("reasoning_content") or "")
            # Light clean only — keep curated verified / agent clips even if short.
            if not caption and _is_curated_keep(v):
                caption = (v.get("reasoning_content") or "").strip()
            videos.append(
                {
                    "original_video": v.get("original_video"),
                    "camera_id": v.get("camera_id"),
                    "location": v.get("location"),
                    "source": source,
                    "reasoning_content": caption,
                    "segment_start_sec": v.get("segment_start_sec"),
                    "segment_end_sec": v.get("segment_end_sec"),
                    "similarity_score": v.get("score") or v.get("similarity_score"),
                    "discovered": bool(v.get("discovered")),
                    "stream_path": f"api/stream?source={quote(source, safe='')}" if source else None,
                }
            )
        enriched = enrich_case(
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "summary": c.get("summary"),
                "recommended_action": c.get("recommended_action"),
                "videos": videos,
            }
        )
        out.append(enriched)
    return out


init_yolo(_login, VSS_URL)
app.register_blueprint(yolo_bp)
app.register_blueprint(init_findmore(_login, VSS_URL))


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


@app.get("/feat_yolo.js")
def feat_yolo_js():
    return send_from_directory(HERE, "feat_yolo.js", mimetype="application/javascript")


@app.get("/feat_findmore.js")
def feat_findmore_js():
    return send_from_directory(HERE, "feat_findmore.js", mimetype="application/javascript")


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
  --amber: #e8a317; --amber-dim: #8a6210; --line: #2c3240; --green: #6bbf7a;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: "IBM Plex Sans", sans-serif; color: var(--ink);
  background:
    radial-gradient(ellipse 80% 50% at 10% -10%, #2a2418 0%, transparent 55%),
    linear-gradient(180deg, var(--bg0), #0c0e12 100%);
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 48px; }
.brand {
  font-family: "Bebas Neue", sans-serif;
  font-size: clamp(3rem, 8vw, 5rem); letter-spacing: 0.04em; line-height: 0.9;
  color: var(--amber); margin: 0 0 8px;
}
.tagline { color: var(--muted); max-width: 42rem; margin: 0 0 20px; }
.meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 16px; }
.layout { display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 18px; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
.case { border-top: 2px solid var(--amber-dim); margin-bottom: 8px; }
.case-head {
  font-family: "Bebas Neue", sans-serif; font-size: 1.45rem; letter-spacing: 0.03em;
  color: var(--amber); padding: 12px 4px 6px; margin: 0; cursor: pointer;
}
.case-sub { color: var(--muted); font-size: 0.8rem; padding: 0 4px 8px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.item {
  padding: 10px 8px; border-bottom: 1px solid var(--line); cursor: pointer;
}
.item:hover, .item.active { background: rgba(232,163,23,0.06); }
.item h4 { margin: 0 0 4px; font-size: 0.92rem; font-weight: 600; }
.item p { margin: 0; color: var(--muted); font-size: 0.8rem; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.badge {
  display: inline-block; font-size: 0.68rem; font-weight: 600; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 2px 6px; border: 1px solid var(--amber-dim);
  color: var(--amber); margin-left: 6px; vertical-align: middle;
}
.panel { background: var(--bg1); border: 1px solid var(--line); padding: 14px; position: sticky; top: 12px; }
.video-wrap { position: relative; width: 100%; background: #000; aspect-ratio: 16/9; }
.video-wrap video { width: 100%; height: 100%; display: block; background: #000; }
.video-wrap canvas {
  position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none;
}
.yolo-status { color: var(--muted); font-size: 0.75rem; margin: 6px 0 0; min-height: 1.1em; }
.brief {
  margin-top: 12px; padding: 12px; border: 1px solid var(--line); background: rgba(0,0,0,0.25);
}
.brief h3 {
  font-family: "Bebas Neue", sans-serif; letter-spacing: 0.04em; color: var(--amber);
  margin: 0 0 8px; font-size: 1.2rem;
}
.brief .stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 10px; }
.stat {
  font-size: 0.75rem; color: var(--muted); border: 1px solid var(--line);
  padding: 4px 8px; background: var(--bg0);
}
.stat b { color: var(--ink); font-weight: 600; }
.brief .summary { margin: 0 0 10px; font-size: 0.9rem; line-height: 1.4; }
.brief .action {
  margin: 0; font-size: 0.88rem; line-height: 1.4; color: var(--ink);
  border-left: 3px solid var(--amber); padding-left: 10px;
}
.brief .action span { display: block; color: var(--amber); font-size: 0.72rem;
  letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 4px; font-weight: 600; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
button.btn {
  font: inherit; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  border: 1px solid var(--amber-dim); background: transparent; color: var(--amber);
  padding: 8px 12px;
}
button.btn:hover { background: rgba(232,163,23,0.12); }
button.btn:disabled { opacity: 0.5; cursor: wait; }
button.btn.primary { background: var(--amber); color: #12141a; border-color: var(--amber); }
button.btn.primary:hover { filter: brightness(1.05); }
.reason { margin-top: 12px; font-size: 0.9rem; line-height: 1.45; max-height: 12rem; overflow: auto; }
.empty { color: var(--muted); padding: 20px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1 class="brand">CrossWatch</h1>
  <p class="tagline">Jaywalk case agent — classify illegal walks, confirm with YOLO on the clip, find more in the archive, and recommend a street fix.</p>
  <div class="meta" id="statusLine">Loading…</div>
  <div class="layout">
    <div id="queue"></div>
    <div class="panel">
      <div class="video-wrap">
        <video id="player" controls playsinline></video>
        <canvas id="yoloCanvas" aria-hidden="true"></canvas>
      </div>
      <div class="yolo-status" id="yoloStatus"></div>
      <div class="brief" id="brief">
        <h3>Case brief</h3>
        <p class="summary" style="color:var(--muted)">Select a case or clip.</p>
      </div>
      <div class="actions">
        <button class="btn primary" id="findMoreBtn" type="button" disabled>Find more like this</button>
      </div>
      <div class="reason" id="reason">Select a clip.</div>
    </div>
  </div>
</div>
<script src="feat_yolo.js"></script>
<script src="feat_findmore.js"></script>
<script>
const BASE = (window.location.pathname.match(/^\/app\/crosswatch(?=\/|$)/) || window.location.pathname.match(/^\/crosswatch(?=\/|$)/) || [""])[0];
const api = (path) => `${BASE}/${path.replace(/^\//, "")}`;
const queueEl = document.getElementById("queue");
const player = document.getElementById("player");
const reason = document.getElementById("reason");
const statusLine = document.getElementById("statusLine");
const briefEl = document.getElementById("brief");
const yoloStatus = document.getElementById("yoloStatus");
const findMoreBtn = document.getElementById("findMoreBtn");
let cases = [];
let selectedKey = null;
let selectedCi = null;
let findBusy = false;

attachYoloOverlay(player, document.getElementById("yoloCanvas"));

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function keyOf(ci, vi) { return ci + ":" + vi; }

function cityLabel(cities) {
  return (cities || []).map(c => String(c).replace(/_/g, " ")).join(", ") || "—";
}

function renderBrief(c) {
  if (!c) {
    briefEl.innerHTML = `<h3>Case brief</h3><p class="summary" style="color:var(--muted)">Select a case or clip.</p>`;
    return;
  }
  const discovered = (c.videos || []).filter(v => v.discovered).length;
  briefEl.innerHTML = `
    <h3>${esc(c.title)}</h3>
    <div class="stats">
      <div class="stat"><b>${c.video_count || 0}</b> clips</div>
      <div class="stat"><b>${c.camera_count || 0}</b> cameras</div>
      <div class="stat"><b>${esc(cityLabel(c.cities))}</b></div>
      ${discovered ? `<div class="stat"><b>${discovered}</b> found via search</div>` : ""}
    </div>
    <p class="summary">${esc(c.summary || "")}</p>
    <p class="action"><span>Recommended action</span>${esc(c.recommended_action || "")}</p>`;
}

function render() {
  queueEl.innerHTML = cases.map((c, ci) => `
    <section class="case" data-case="${ci}">
      <h2 class="case-head" data-ci="${ci}">${String(ci+1).padStart(2,"0")} · ${esc(c.title)}</h2>
      <div class="case-sub">
        <span>${c.video_count || (c.videos||[]).length} clips · ${c.camera_count || 0} cams · ${esc(cityLabel(c.cities))}</span>
      </div>
      ${(c.videos || []).map((v, vi) => `
        <div class="item ${selectedKey === keyOf(ci,vi) ? "active" : ""}" data-ci="${ci}" data-vi="${vi}">
          <h4>${esc(v.camera_id)} · ${esc(v.location)}${v.discovered ? '<span class="badge">New</span>' : ""}</h4>
          <p>${esc(v.reasoning_content)}</p>
        </div>`).join("")}
    </section>`).join("") || '<p class="empty">No cases loaded.</p>';
  queueEl.querySelectorAll(".item").forEach(el => {
    el.addEventListener("click", () => select(+el.dataset.ci, +el.dataset.vi));
  });
  queueEl.querySelectorAll(".case-head").forEach(el => {
    el.addEventListener("click", () => {
      const ci = +el.dataset.ci;
      selectedCi = ci;
      renderBrief(cases[ci]);
      findMoreBtn.disabled = findBusy;
      if (cases[ci].videos && cases[ci].videos[0]) select(ci, 0);
    });
  });
}

async function select(ci, vi) {
  const c = cases[ci];
  const v = c.videos[vi];
  selectedKey = keyOf(ci, vi);
  selectedCi = ci;
  renderBrief(c);
  findMoreBtn.disabled = findBusy;
  if (v.stream_path) {
    player.src = api(v.stream_path);
    player.play().catch(() => {});
  }
  const score = v.similarity_score != null ? `<br/>sim ${Number(v.similarity_score).toFixed(3)}` : "";
  reason.innerHTML = `<strong>${esc(c.title)}</strong>${v.discovered ? ' · archive find' : ''}<br/>${esc(v.camera_id)} · ${esc(v.location)}${score}<br/><br/>${esc(v.reasoning_content)}`;
  yoloStatus.textContent = "Loading YOLO…";
  setYoloDetections({ frames: [], status: "missing" });
  render();
  try {
    const data = await loadDetections(BASE, v.source);
    const n = (data.frames || []).reduce((a, f) => a + ((f.detections || []).length), 0);
    yoloStatus.textContent = data.status === "missing"
      ? "No YOLO sidecar for this segment"
      : `YOLO · ${(data.frames || []).length} frames · ${n} boxes`;
  } catch (e) {
    yoloStatus.textContent = "YOLO unavailable: " + e.message;
    setYoloDetections({ frames: [], status: "missing" });
  }
}

findMoreBtn.addEventListener("click", async () => {
  if (selectedCi == null || findBusy) return;
  const c = cases[selectedCi];
  findBusy = true;
  findMoreBtn.disabled = true;
  findMoreBtn.textContent = "Searching archive…";
  statusLine.textContent = `Finding more for ${c.title}…`;
  try {
    const exclude = (c.videos || []).map(v => v.source).filter(Boolean);
    const data = await findMoreLikeThis(BASE, c.id, exclude);
    const added = data.results || [];
    c.videos.push(...added);
    const enriched = {
      ...c,
      video_count: c.videos.length,
      camera_count: new Set(c.videos.map(v => v.camera_id).filter(Boolean)).size,
      cities: [...new Set(c.videos.map(v => v.location).filter(Boolean))].sort(),
    };
    cases[selectedCi] = enriched;
    statusLine.textContent = added.length
      ? `Added ${added.length} clip(s) to ${c.title} · query: ${data.query}`
      : `No new clips for ${c.title} (already have matching sources or none above threshold)`;
    render();
    renderBrief(cases[selectedCi]);
    if (added.length) {
      select(selectedCi, cases[selectedCi].videos.length - added.length);
    }
  } catch (e) {
    statusLine.textContent = "Find more failed: " + e.message;
  } finally {
    findBusy = false;
    findMoreBtn.textContent = "Find more like this";
    findMoreBtn.disabled = selectedCi == null;
  }
});

async function load() {
  try {
    const res = await fetch(api("api/cases"));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "load failed");
    cases = data.results || [];
    statusLine.textContent = `${data.count} cases · ${data.video_total} clips · YOLO overlay · archive search`;
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
