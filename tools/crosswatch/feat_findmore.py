"""Feature 4 — Find more like this via VSS hybrid search."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, request

from feat_quality import clean_caption, filter_videos

SEARCH_QUERIES: dict[str, str] = {
    "red_hand": (
        "pedestrian crossing marked crosswalk on red hand don't-walk signal "
        "illegal walk against pedestrian light"
    ),
    "against_traffic": (
        "pedestrian crossing intersection while cars have green light and "
        "vehicles are moving through conflict zone"
    ),
    "diagonal": (
        "pedestrian cutting diagonally across intersection off marked "
        "crosswalk lines jaywalking corner cut"
    ),
    "mid_block": (
        "pedestrian crossing mid-block outside marked crosswalk darting "
        "across street between blocks"
    ),
    "between_cars": (
        "pedestrian stepping into moving traffic between cars away from "
        "crosswalk weaving through vehicle gaps"
    ),
}


def search_more(
    vss_url: str,
    token: str,
    case_id: str,
    exclude_sources: set[str],
    top_k: int = 8,
) -> list[dict[str, Any]]:
    """Search VSS for more clips of this case type; map to CrossWatch video shape."""
    query = SEARCH_QUERIES.get(case_id)
    if not query:
        raise ValueError(f"unknown case_id: {case_id}")

    base = vss_url.rstrip("/")
    r = requests.post(
        f"{base}/api/v1/search",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "top_k": top_k,
            "min_similarity": 0.32,
            "llm_top_n": 1,  # must be >= 1
            "include_public": True,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()

    out: list[dict[str, Any]] = []
    for hit in data.get("results") or []:
        source = hit.get("source") or ""
        if not source or source in exclude_sources:
            continue
        caption = clean_caption(hit.get("reasoning_content") or "")
        out.append(
            {
                "original_video": hit.get("original_video"),
                "camera_id": hit.get("camera_id"),
                "location": hit.get("location"),
                "source": source,
                "reasoning_content": caption,
                "segment_start_sec": hit.get("segment_start_sec"),
                "segment_end_sec": hit.get("segment_end_sec"),
                "stream_path": f"api/stream?source={quote(source, safe='')}",
                "similarity_score": hit.get("similarity_score"),
                "discovered": True,
            }
        )
    return filter_videos(case_id, out)


def init_findmore(login_fn: Callable[[], str], vss_url: str) -> Blueprint:
    """Return a Blueprint with POST /api/find-more. Register on the Flask app."""
    bp = Blueprint("findmore", __name__)

    @bp.post("/api/find-more")
    def find_more():
        body = request.get_json(force=True, silent=True) or {}
        case_id = (body.get("case_id") or "").strip()
        if not case_id:
            return jsonify({"error": "case_id required"}), 400
        if case_id not in SEARCH_QUERIES:
            return jsonify({"error": f"unknown case_id: {case_id}"}), 400

        exclude = set(body.get("exclude_sources") or [])
        query = SEARCH_QUERIES[case_id]

        try:
            token = login_fn()
            results = search_more(vss_url, token, case_id, exclude)
        except requests.HTTPError as e:
            detail = str(e)
            try:
                detail = e.response.json().get("detail") or e.response.text or detail
            except Exception:
                pass
            return jsonify({"error": detail}), 502
        except requests.RequestException as e:
            return jsonify({"error": str(e)}), 502
        except Exception as e:
            return jsonify({"error": str(e)}), 502

        return jsonify(
            {
                "case_id": case_id,
                "query": query,
                "count": len(results),
                "results": results,
            }
        )

    return bp
