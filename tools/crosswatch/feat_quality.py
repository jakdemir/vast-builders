"""Caption quality — clean rambles and reject contradictory archive hits."""

from __future__ import annotations

import re
from typing import Any

LEGAL = re.compile(
    r"\b(crossing legally|legal[ly]? (marked )?crosswalk|right of way|"
    r"no (visible )?(illegal|unusual)|not (cutting|crossing|entering|stepping)|"
    r"safe for pedestrians|walk phase|walking figure|pedestrians have the right|"
    r"no one is (cutting|crossing|stepping|entering)|none_observed|legal_crosswalk)\b",
    re.I,
)
NEGATE_ILLEGAL = re.compile(
    r"\b(not|no|without|none)\b.{0,40}\b(illegal|red hand|don't-walk|dont-walk|"
    r"against (the )?signal|against traffic|diagonal|mid-?block|between cars|jaywalk)",
    re.I,
)

CASE_POS: dict[str, list[re.Pattern[str]]] = {
    "red_hand": [
        re.compile(r"\bred hand\b", re.I),
        re.compile(r"\bdon'?t-?walk\b", re.I),
        re.compile(r"\bpedestrian signal\b.{0,40}\bred\b", re.I),
        re.compile(r"\bagainst (the )?(pedestrian )?(signal|light)\b", re.I),
        re.compile(r"\bcrossing .{0,40}(on|during|while).{0,20}(red hand|don't-walk)\b", re.I),
    ],
    "against_traffic": [
        re.compile(r"\b(against traffic|into (moving )?traffic|through (moving )?traffic)\b", re.I),
        re.compile(r"\b(vehicles|cars|traffic).{0,40}\b(green|moving).{0,40}\b(cross|pedestrian)\b", re.I),
        re.compile(r"\bpedestrian.{0,50}\b(while|as).{0,30}\b(cars|vehicles).{0,20}\b(move|moving|green)\b", re.I),
        re.compile(r"\bcrossing .{0,40}\b(oncoming|moving) (cars|vehicles|traffic)\b", re.I),
    ],
    "diagonal": [
        re.compile(r"\bdiagonal(ly)?\b", re.I),
        re.compile(r"\bcut(ting)? (across|through) (the )?intersection\b", re.I),
        re.compile(r"\boff (the )?(marked )?crosswalk\b", re.I),
        re.compile(r"\bcrosswalk lines\b", re.I),
    ],
    "mid_block": [
        re.compile(r"\bmid-?block\b", re.I),
        re.compile(r"\boutside (a |the )?(marked )?crosswalk\b", re.I),
        re.compile(r"\bbetween (the )?blocks\b", re.I),
        re.compile(r"\bnot (at|in) (a |the )?(marked )?crosswalk\b", re.I),
        re.compile(r"\bjaywalk\b", re.I),
    ],
    "between_cars": [
        re.compile(r"\bbetween (the )?(cars|vehicles)\b", re.I),
        re.compile(r"\bweav(e|ing) through\b", re.I),
        re.compile(r"\bgaps? (in |between ).{0,20}(cars|traffic|vehicles)\b", re.I),
        re.compile(r"\binto (the )?path of (a |moving )?(car|vehicle|traffic)\b", re.I),
        re.compile(r"\bstepping into (moving )?traffic\b", re.I),
    ],
}


def clean_caption(text: str, max_chars: int = 420) -> str:
    """Dedupe near-repeated sentences and trim long rambles."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", p.lower()).strip()
        sig = key[:80]
        if sig in seen:
            continue
        seen.add(sig)
        out.append(p)
    text = " ".join(out)
    if len(text) > max_chars:
        cut = text[:max_chars]
        if ". " in cut:
            cut = cut.rsplit(". ", 1)[0] + "."
        else:
            cut = cut.rstrip() + "…"
        text = cut
    return text


def score_caption(case_id: str, caption: str) -> tuple[float, str]:
    """Score caption support for a case. Hard-reject legal/contradiction phrases."""
    cap = caption or ""
    if not cap.strip():
        return -2.0, "empty"
    if LEGAL.search(cap) or NEGATE_ILLEGAL.search(cap):
        return -5.0, "contradicts_or_legal"
    cues = CASE_POS.get(case_id) or []
    pos = sum(1 for rx in cues if rx.search(cap))
    if pos == 0:
        return -1.0, "no_support"
    score = float(pos)
    if len(cap) < 500:
        score += 0.2
    return score, f"support_x{pos}"


def filter_videos(case_id: str, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep videos whose cleaned caption scores >= 1 for case_id."""
    kept: list[dict[str, Any]] = []
    for v in videos or []:
        raw = v.get("reasoning_content") or ""
        cleaned = clean_caption(raw)
        score, reason = score_caption(case_id, cleaned)
        if score < 1.0:
            continue
        row = dict(v)
        row["reasoning_content"] = cleaned
        row["caption_support"] = reason
        kept.append(row)
    return kept
