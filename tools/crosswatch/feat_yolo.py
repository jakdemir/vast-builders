"""CrossWatch Feature 3 — YOLO detections proxy (Blueprint + helpers).

Wire into Flask (parent merge)::

    from feat_yolo import yolo_bp, init_yolo
    init_yolo(_login, VSS_URL)
    app.register_blueprint(yolo_bp)
"""

from __future__ import annotations

from typing import Any, Callable

import requests
from flask import Blueprint, jsonify, request

LoginFn = Callable[..., str]

yolo_bp = Blueprint("yolo", __name__)

_login: LoginFn | None = None
_vss_url: str = ""


def init_yolo(login_fn: LoginFn, vss_url: str) -> None:
    """Set module-level login callback and VSS base URL for the Blueprint."""
    global _login, _vss_url
    _login = login_fn
    _vss_url = (vss_url or "").rstrip("/")


def register_yolo_routes(
    app: Any,
    login_fn: LoginFn,
    vss_url_getter: Callable[[], str] | str,
) -> None:
    """Optional one-shot register: init + attach Blueprint to ``app``."""
    url = vss_url_getter() if callable(vss_url_getter) else vss_url_getter
    init_yolo(login_fn, url)
    app.register_blueprint(yolo_bp)


def _normalize_bbox(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        return [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
    except (TypeError, ValueError):
        return None


def _normalize_shape(raw: Any, fallback: list[float] | None = None) -> list[float]:
    """Return ``[h, w]`` matching VSS / roadplay convention."""
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            h, w = float(raw[0]), float(raw[1])
            if h > 0 and w > 0:
                return [h, w]
        except (TypeError, ValueError):
            pass
    if fallback and len(fallback) >= 2:
        return [float(fallback[0]), float(fallback[1])]
    return [1080.0, 1920.0]


def normalize_detections(payload: Any) -> dict[str, Any]:
    """Normalize VSS detections JSON to CrossWatch overlay shape."""
    if not isinstance(payload, dict):
        return {"status": "ok", "fps": 30.0, "frames": []}

    fps = float(payload.get("fps") or 30.0)
    if fps <= 0:
        fps = 30.0
    video_shape = _normalize_shape(payload.get("video_shape"))
    raw_frames = payload.get("frames") or []
    frames: list[dict[str, Any]] = []

    for i, fr in enumerate(raw_frames if isinstance(raw_frames, list) else []):
        if not isinstance(fr, dict):
            continue
        shape = _normalize_shape(fr.get("shape"), video_shape)
        if fr.get("time_sec") is not None:
            try:
                t = float(fr["time_sec"])
            except (TypeError, ValueError):
                t = i / fps
        else:
            try:
                fi = float(fr.get("frame_index") or i)
            except (TypeError, ValueError):
                fi = float(i)
            t = fi / fps

        dets_out: list[dict[str, Any]] = []
        for d in fr.get("detections") or []:
            if not isinstance(d, dict):
                continue
            bbox = _normalize_bbox(d.get("bbox"))
            if not bbox:
                continue
            try:
                conf = float(d.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            dets_out.append(
                {
                    "label": str(d.get("label") or "object"),
                    "confidence": conf,
                    "bbox": bbox,
                }
            )
        frames.append(
            {
                "time_sec": t,
                "shape": shape,
                "detections": dets_out,
            }
        )

    return {"status": "ok", "fps": fps, "frames": frames}


def fetch_detections(vss_url: str, token: str, source: str) -> dict[str, Any]:
    """GET VSS ``/api/v1/videos/detections?source=``.

    404 (no sidecar) → ``{"frames": [], "status": "missing"}``.
    Other HTTP errors raise ``requests.HTTPError``.
    """
    base = (vss_url or "").rstrip("/")
    r = requests.get(
        f"{base}/api/v1/videos/detections",
        params={"source": source},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    if r.status_code == 404:
        return {"status": "missing", "fps": 0.0, "frames": []}
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        return {"status": "ok", "fps": 30.0, "frames": []}
    return normalize_detections(body)


@yolo_bp.get("/api/detections")
def api_detections():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "source required"}), 400
    if not _login or not _vss_url:
        return jsonify({"error": "yolo not initialized; call init_yolo first"}), 500
    try:
        token = _login()
        data = fetch_detections(_vss_url, token, source)
        return jsonify(data), 200
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        detail = ""
        if e.response is not None:
            try:
                detail = e.response.text[:300]
            except Exception:
                detail = str(e)
        return jsonify({"error": f"vss detections failed ({status})", "detail": detail}), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500
