#!/usr/bin/env python3
"""Team project catalog — always the first page at /app."""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request

PORT = int(os.environ.get("PORT", "8080"))
PROJECTS_PATH = Path(__file__).with_name("projects.json")

app = Flask(__name__)


def _load() -> dict:
    return json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "team-catalog"})


@app.get("/api/projects")
def api_projects():
    data = _load()
    return jsonify(data)


@app.post("/api/projects")
def api_add_project():
    """Register another team project on the catalog (idempotent by id)."""
    body = request.get_json(force=True, silent=True) or {}
    pid = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    path = (body.get("path") or "").strip()
    if not pid or not name or not path:
        return jsonify({"error": "id, name, path required"}), 400
    if not path.startswith("/"):
        path = "/" + path
    data = _load()
    projects = data.setdefault("projects", [])
    entry = {
        "id": pid,
        "name": name,
        "path": path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path,
        "blurb": (body.get("blurb") or "").strip(),
        "status": (body.get("status") or "live").strip(),
    }
    for i, p in enumerate(projects):
        if p.get("id") == pid:
            projects[i] = {**p, **entry}
            break
    else:
        projects.append(entry)
    PROJECTS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return jsonify({"ok": True, "projects": projects})


def _render_page() -> str:
    data = _load()
    cards = []
    for p in data.get("projects") or []:
        name = (
            str(p.get("name") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        path = (
            str(p.get("path") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        blurb = (
            str(p.get("blurb") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        cards.append(
            f'<a class="card" href="{path}">'
            f'<div class="row"><h2>{name}</h2><span class="path">{path}</span></div>'
            f"<p>{blurb}</p></a>"
        )
    title = str(data.get("title") or "Builders Catalog")
    subtitle = str(data.get("subtitle") or "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
:root {{
  --bg: #0f1419; --panel: #1a222c; --ink: #eef2f6; --muted: #9aa8b5;
  --accent: #3ecf8e; --line: #2a3540;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; }}
body {{
  font-family: "DM Sans", sans-serif; color: var(--ink);
  background:
    radial-gradient(ellipse 70% 45% at 0% 0%, #1a3a2a 0%, transparent 55%),
    radial-gradient(ellipse 50% 40% at 100% 10%, #1a2838 0%, transparent 50%),
    var(--bg);
}}
.wrap {{ max-width: 920px; margin: 0 auto; padding: 40px 22px 64px; }}
.brand {{
  font-family: Syne, sans-serif; font-weight: 800;
  font-size: clamp(2.6rem, 7vw, 4.2rem); letter-spacing: -0.03em;
  line-height: 0.95; margin: 0 0 12px;
}}
.brand span {{ color: var(--accent); }}
.sub {{ color: var(--muted); max-width: 34rem; margin: 0 0 36px; font-size: 1.05rem; line-height: 1.45; }}
.grid {{ display: grid; gap: 14px; }}
.card {{
  display: block; text-decoration: none; color: inherit;
  background: var(--panel); border: 1px solid var(--line);
  padding: 20px 22px; transition: border-color .15s, transform .15s;
}}
.card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
.card h2 {{
  font-family: Syne, sans-serif; font-weight: 700; font-size: 1.45rem;
  margin: 0 0 6px; letter-spacing: -0.02em;
}}
.card p {{ margin: 0; color: var(--muted); font-size: 0.95rem; line-height: 1.4; }}
.row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
.path {{ font-size: 0.78rem; color: var(--accent); letter-spacing: 0.02em; }}
.foot {{ margin-top: 36px; color: var(--muted); font-size: 0.85rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1 class="brand">Builders <span>Catalog</span></h1>
  <p class="sub">{subtitle}</p>
  <div class="grid">{"".join(cards)}</div>
  <p class="foot">Workshop <strong>App</strong> opens this catalog. New projects get added here.</p>
</div>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(_render_page(), mimetype="text/html")


def main():
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
            {"bind": f"0.0.0.0:{PORT}", "workers": 1, "threads": 2, "timeout": 60},
        ).run()
    except Exception:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
