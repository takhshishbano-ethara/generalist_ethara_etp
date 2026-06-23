# -*- coding: utf-8 -*-
"""Deterministic pre-LLM checks for image_ab justification consistency.

Ported from the reference CLI evaluator
(sample_data/new_requirements/New project/evaluators/consistency_checker.py).
Dependency-free heuristic validation of a candidate justification against
their selected dimension ratings, run BEFORE any Vertex call so the LLM gets
the flags as supporting signals. ``tasker_ratings`` is keyed by dimension
abbreviation (OC/IF/VQ/LAI/A/B/BG/BB); each value normalizes to A/B/BG/BB/NA.
"""
import re

VQ_KEYWORDS = [
    "sharp", "sharper", "clear", "clearer", "quality", "detail",
    "detailed", "blurry", "crisp", "resolution",
]

LAI_KEYWORDS = [
    "realistic", "natural", "artifact", "artifacts", "distortion",
    "distortions", "warped", "deformed", "less ai", "more ai", "fake",
]

OC_POSITIVE_KEYWORDS = [
    "overall", "better", "best", "preferred", "preference", "choose",
    "chosen", "winner", "wins",
]

SEVERITY_ORDER = {
    "none": 0,
    "minor": 1,
    "major": 2,
    "critical": 3,
}

JUSTIFIED_DIMENSIONS = {"A", "B", "BB"}
OPTIONAL_DIMENSIONS = {"BG"}


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _contains_any_keyword(text, keywords):
    normalized = normalize(text)
    for keyword in keywords:
        if " " in keyword:
            if keyword in normalized:
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            return True
    return False


def _normalize_rating(value):
    if value is None:
        return ""
    text = normalize(str(value)).upper()
    return re.sub(r"[^A-Z]", "", text)


def _extract_referenced_dimensions(text):
    normalized = normalize(text)
    referenced = set()
    if re.search(r"\bbg\b|\bbackground\b", normalized):
        referenced.add("BG")
    if re.search(r"\bbb\b", normalized):
        referenced.add("BB")
    if re.search(r"\ba\b", normalized):
        referenced.add("A")
    if re.search(r"\bb\b", normalized):
        referenced.add("B")
    return referenced


def _severity_from_flags(flags):
    highest = 0
    for flag in flags:
        level = SEVERITY_ORDER.get(str(flag.get("severity", "none")), 0)
        highest = max(highest, level)
    for name, level in SEVERITY_ORDER.items():
        if level == highest:
            return name
    return "none"


def detect_preferred_response(text):
    normalized = normalize(text)
    patterns_a = [
        r"\bresponse a\b", r"\bimage a\b", r"\ba\b is better\b",
        r"\bprefer a\b", r"\bpreferred a\b", r"\bchoose a\b",
        r"\ba wins\b", r"\ba is best\b",
    ]
    patterns_b = [
        r"\bresponse b\b", r"\bimage b\b", r"\bb\b is better\b",
        r"\bprefer b\b", r"\bpreferred b\b", r"\bchoose b\b",
        r"\bb wins\b", r"\bb is best\b",
    ]
    a_score = sum(len(re.findall(p, normalized)) for p in patterns_a)
    b_score = sum(len(re.findall(p, normalized)) for p in patterns_b)
    if a_score > b_score:
        return "A"
    if b_score > a_score:
        return "B"
    return None


def check_oc_mismatch(tasker_oc, justification):
    normalized_oc = _normalize_rating(tasker_oc)
    preferred = detect_preferred_response(justification)
    if normalized_oc not in {"A", "B"} or preferred is None \
            or normalized_oc == preferred:
        return []
    return [{
        "code": "oc_mismatch",
        "severity": "critical",
        "message": (
            f"Overall choice is '{normalized_oc}' but the justification "
            f"appears to prefer '{preferred}'."
        ),
    }]


def check_dimension_mismatch(tasker_ratings, justification):
    flags = []
    normalized = normalize(justification)
    vq_rating = _normalize_rating(tasker_ratings.get("VQ"))
    lai_rating = _normalize_rating(tasker_ratings.get("LAI"))
    mentions_vq = _contains_any_keyword(normalized, VQ_KEYWORDS)
    mentions_lai = _contains_any_keyword(normalized, LAI_KEYWORDS)

    if vq_rating in JUSTIFIED_DIMENSIONS and not mentions_vq:
        flags.append({
            "code": "vq_missing_support",
            "severity": "major",
            "message": (
                f"VQ is rated '{vq_rating}' but the justification does not "
                "appear to mention visual quality evidence."
            ),
        })
    if lai_rating in JUSTIFIED_DIMENSIONS and not mentions_lai:
        flags.append({
            "code": "lai_missing_support",
            "severity": "major",
            "message": (
                f"LAI is rated '{lai_rating}' but the justification does not "
                "appear to mention realism or artifact evidence."
            ),
        })
    if vq_rating in {"NA", ""} and mentions_vq:
        flags.append({
            "code": "vq_unrated_but_discussed",
            "severity": "minor",
            "message": "The justification discusses VQ-style evidence but VQ "
                       "is not meaningfully rated.",
        })
    if lai_rating in {"NA", ""} and mentions_lai:
        flags.append({
            "code": "lai_unrated_but_discussed",
            "severity": "minor",
            "message": "The justification discusses LAI-style evidence but LAI "
                       "is not meaningfully rated.",
        })
    return flags


def check_missing_dimension_reasoning(tasker_ratings, justification):
    flags = []
    referenced_dimensions = _extract_referenced_dimensions(justification)
    for dimension in ("A", "B", "BG"):
        rating = _normalize_rating(tasker_ratings.get(dimension))
        if rating not in JUSTIFIED_DIMENSIONS | OPTIONAL_DIMENSIONS:
            continue
        if rating in OPTIONAL_DIMENSIONS:
            continue
        if dimension not in referenced_dimensions:
            flags.append({
                "code": f"{dimension.lower()}_missing_reasoning",
                "severity": "major",
                "message": (
                    f"{dimension} is rated '{rating}' but the justification "
                    f"does not clearly address dimension {dimension}."
                ),
            })
    return flags


def check_bb_reasoning(tasker_ratings, justification):
    bb_rating = _normalize_rating(tasker_ratings.get("BB"))
    if bb_rating not in JUSTIFIED_DIMENSIONS:
        return []
    referenced_dimensions = _extract_referenced_dimensions(justification)
    if "BB" in referenced_dimensions:
        return []
    return [{
        "code": "bb_missing_reasoning",
        "severity": "major",
        "message": (
            f"BB is rated '{bb_rating}' but the justification does not clearly "
            "address BB-specific reasoning."
        ),
    }]


def consistency_checker(tasker_ratings, justification):
    safe_justification = justification or ""
    flags = []
    flags.extend(check_oc_mismatch(
        tasker_ratings.get("OC"), safe_justification))
    flags.extend(check_dimension_mismatch(tasker_ratings, safe_justification))
    flags.extend(check_missing_dimension_reasoning(
        tasker_ratings, safe_justification))
    flags.extend(check_bb_reasoning(tasker_ratings, safe_justification))
    return {
        "flags": flags,
        "severity": _severity_from_flags(flags),
    }
