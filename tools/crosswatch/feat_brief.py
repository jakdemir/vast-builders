"""Case brief enrichment for CrossWatch — summary, action, and clip/camera/city counts."""

from __future__ import annotations

CASE_FALLBACKS: dict[str, dict[str, str]] = {
    "red_hand": {
        "summary": (
            "Pedestrians enter or continue through a marked crosswalk while the "
            "pedestrian signal shows a red hand or don't-walk."
        ),
        "recommended_action": (
            "Extend pedestrian clearance (LPI + longer Walk) and add high-visibility "
            "countdown timers so walk phases match actual crossing demand."
        ),
    },
    "against_traffic": {
        "summary": (
            "Pedestrians cross an intersection while vehicles have a green signal "
            "and are moving through the conflict zone."
        ),
        "recommended_action": (
            "Add Leading Pedestrian Interval (LPI) and tighten turn-on-red / "
            "protected-only lefts to give walkers a head start before conflicting traffic moves."
        ),
    },
    "diagonal": {
        "summary": (
            "Pedestrians cut diagonally across an intersection, leaving the marked "
            "crosswalk lines and shortening the path through open pavement."
        ),
        "recommended_action": (
            "Install scramble (Barnes Dance) phasing or corner curb extensions that "
            "make the legal two-leg path shorter and more obvious than a diagonal cut."
        ),
    },
    "mid_block": {
        "summary": (
            "Pedestrians cross mid-block outside a marked crosswalk, often where the "
            "nearest legal crossing feels too far."
        ),
        "recommended_action": (
            "Add a midblock marked crosswalk with raised table or midblock fence/"
            "planting strip that channels desire lines to a controlled crossing."
        ),
    },
    "between_cars": {
        "summary": (
            "Pedestrians step into moving traffic between vehicles away from a marked "
            "crosswalk, weaving through gaps in the stream."
        ),
        "recommended_action": (
            "Calm midblock speeds with turn calming / pinch points and continuous "
            "sidewalk fencing so gaps between cars are not a usable crossing path."
        ),
    },
}


def enrich_case(case: dict) -> dict:
    """Return a shallow copy of case with brief fields and aggregate counts."""
    out = dict(case)
    case_id = out.get("id") or ""
    fallback = CASE_FALLBACKS.get(case_id, {})

    out["summary"] = out.get("summary") or fallback.get("summary", "")
    out["recommended_action"] = (
        out.get("recommended_action") or fallback.get("recommended_action", "")
    )

    videos = out.get("videos") or []
    cameras = {v.get("camera_id") for v in videos if v.get("camera_id")}
    cities = sorted({v.get("location") for v in videos if v.get("location")})

    out["camera_count"] = len(cameras)
    out["cities"] = cities
    out["video_count"] = len(videos)
    return out
