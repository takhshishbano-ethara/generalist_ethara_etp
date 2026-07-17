# -*- coding: utf-8 -*-

import json
import re

DEFAULT_SUBJECTIVE_THRESHOLD = 70.0

AB_VERDICT_WEIGHT = 0.75
AB_JUSTIFICATION_WEIGHT = 0.25

# Overridable per prefix via ir.config_parameter ``etp_assessment_pro.tag_weight_<prefix>``.
TAG_PREFIX_WEIGHTS = {
    "task": 3.0,
    "domain": 2.0,
    "skill": 2.0,
    "modality": 1.0,
    "output-format": 1.0,
}
TAG_DEFAULT_PREFIX_WEIGHT = 1.0
# Threshold on the total weight of SHARED tags, NOT the Jaccard ratio.
# Override via ir.config_parameter ``etp_assessment_pro.tag_similar_min_score``.
TAG_SIMILAR_MIN_SCORE_DEFAULT = 2.0

# Postgres advisory-lock keys: MUST stay mutually unique across the whole module.
ADVISORY_LOCK_AUTOSCORE = 827193
ADVISORY_LOCK_IMAGE_RENDER = 827194
ADVISORY_LOCK_IMAGE_DETECT = 827195
ADVISORY_LOCK_VIDEO_POLL = 827196
ADVISORY_LOCK_SKILL_EXTRACT = 827200
ADVISORY_LOCK_QUESTION_GEN = 827201
ADVISORY_LOCK_TAG_EXTRACT = 827202
ADVISORY_LOCK_VERTEX_BEARER = 827300

QUESTION_TYPE_SELECTION = [
    ("mcq", "Objective - MCQ"),
    ("msq", "Objective - MSQ"),
    ("subjective_rubric", "Subjective - Rubric"),
    ("image_ab", "Image - A/B Evaluation"),
    ("image_prompt", "Image - Prompt"),
    ("image_label", "Image - Labelling"),
    ("video_prompt", "Video - Prompt"),
]
QUESTION_TYPE_CODES = frozenset(code for code, _label in QUESTION_TYPE_SELECTION)
# Deterministic order for prompt text; QUESTION_TYPE_CODES is unordered.
QUESTION_TYPE_ORDER = tuple(code for code, _label in QUESTION_TYPE_SELECTION)

OBJECTIVE_QUESTION_TYPES = frozenset({"mcq", "msq"})
SUBJECTIVE_QUESTION_TYPES = frozenset({"subjective_rubric"})
IMAGE_QUESTION_TYPES = frozenset({"image_ab", "image_prompt", "image_label"})
VIDEO_QUESTION_TYPES = frozenset({"video_prompt"})

DETECTION_MODE_SELECTION = [("object", "Objects"), ("ui", "UI Elements")]

QUESTION_TYPE_SHORT_LABELS = {
    "mcq": "MCQ",
    "msq": "MSQ",
    "subjective_rubric": "Rubric",
    "image_ab": "Image Comparison",
    "image_prompt": "Image Prompt",
    "image_label": "Image Labelling",
    "video_prompt": "Video Prompt",
}

QUESTION_TYPE_PROMPT_LIST = "/".join(code for code, _label in QUESTION_TYPE_SELECTION)


DIFFICULTY_SELECTION = [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")]
DIFFICULTY_CODES = frozenset(code for code, _label in DIFFICULTY_SELECTION)


MEDIUM_SELECTION = [("text", "Text"), ("image", "Image"), ("video", "Video")]
MEDIUM_CODES = frozenset(code for code, _label in MEDIUM_SELECTION)


AB_DIMENSIONS = [
    ("IF", "Instruction Following"),
    ("VQ", "Visual Quality"),
    ("LAI", "Less AI Generated"),
    ("OC", "Overall Choice"),
]
AB_DIMENSION_NAMES = {code: name for code, name in AB_DIMENSIONS}
AB_DIMENSION_CODES = frozenset(code for code, _name in AB_DIMENSIONS)

AB_CHOICES = ["Response A", "Response B", "Both Good", "Both Bad"]
AB_CHOICE_SET = frozenset(AB_CHOICES)


VERTEX_GLOBAL_LOCATION = "global"
VERTEX_DEFAULT_LOCATION = VERTEX_GLOBAL_LOCATION
VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"
VERTEX_DEFAULT_IMAGE_MODEL = VERTEX_DEFAULT_MODEL
# Generation reads a SOP document: must NOT fall back to the image model, which
# sees a PDF as opaque binary -> "document has no pages".
GENERATION_DEFAULT_MODEL = "gemini-3.1-pro-preview"

# Veo is served ONLY on a regional endpoint; the gemini 'global' location 404s
# for Veo, hence its own location constant.
VIDEO_DEFAULT_MODEL = "veo-3.1-generate-001"
VIDEO_DEFAULT_LOCATION = "us-central1"
VIDEO_DEFAULT_DURATION_S = 6


_OPTION_REASONING_RE = re.compile(
    r",\s+(because|since|due to|as it|making it|so that|therefore|"
    r"which makes|as the|given that)\b", re.IGNORECASE)


def option_name_reveals_reasoning(name):
    if not name:
        return False
    match = _OPTION_REASONING_RE.search(name)
    return bool(match) and len(name[:match.start()].split()) <= 5


_SOURCE_REF_RE = re.compile(
    r"\bSOPs?\b"
    r"|\bstandard operating procedures?\b"
    r"|\b(?:Section|Sub-?section|Step|Clause|Rule)\s+\d"
    r"|\b(?:according to|as per|per|as (?:stated|specified|defined|described|"
    r"outlined|set out|laid out|mentioned) in|in accordance with|pursuant to|"
    r"as required by|in line with)\s+the\s+(?:\w+\s+){0,2}"
    r"(?:guidelines?|documentation|document|material|polic(?:y|ies)|"
    r"procedures?|manual|handbook|sop|protocol|spec(?:ification)?s?|"
    r"rubric|criteria|scheme|brief|playbook|runbook|standards?|"
    r"workflows?|process(?:es)?|framework)\b",
    re.IGNORECASE)


def text_has_source_reference(*texts):
    for text in texts:
        if text and _SOURCE_REF_RE.search(text):
            return True
    return False


# Patterns are compiled with re.IGNORECASE by services/gates.py; a match resolves
# the answer to raw score 0 locally and the grader is never called.
# Keep every entry TIGHT: it must match a "coerce the grader" idiom, never ordinary
# answer prose. A broad pattern silently zeroes honest candidates, with no error.
INTEGRITY_GATE_PATTERNS = [
    r"ignore\s+(?:the\s+|this\s+|all\s+)?rubric",
    r"ignore\s+(?:all\s+)?(?:previous\s+|prior\s+|the\s+above\s+)?instructions",
    r"disregard\s+(?:the\s+|all\s+|any\s+)?(?:previous\s+|prior\s+)?instructions",
    r"(?:grading|scoring)\s+is\s+waived",
    r"you\s+are\s+now\b",
    r"award\s+(?:full|maximum|max)\s+(?:marks|score|points|credit)",
    r"(?:output|print|return|give|assign|set)\b.{0,40}?\bscore\b.{0,20}?"
    r"(?:1\.0|100|full|max(?:imum)?)",
    r"(?:score|grade)\s+(?:of\s+)?(?:1\.0|100)\b",
]

# 0-100 upper bounds applied by services/scoring._apply_ceilings; must stay in
# sync with the self-applied caps in prompts/scoring.md.
SCORE_CEILINGS = {
    "verdict_contradiction": 25.0,
    "multi_checklist_zero": 55.0,
    "fabrication": 25.0,
}


AB_FLAWED_SIDES = ("a", "b")

_AB_CODE_PAREN_RE = re.compile(r"\(([A-Za-z]{1,4})\)")


def ab_side_verdict(side):
    return "Response A" if (side or "").strip().lower() == "a" else "Response B"


def ab_other_side(side):
    return "b" if (side or "").strip().lower() == "a" else "a"


def ab_code_from_label(label):
    if not label:
        return ""
    m = _AB_CODE_PAREN_RE.search(label)
    if m:
        code = m.group(1).upper()
        if code in AB_DIMENSION_CODES:
            return code
    s = (label or "").strip()
    if s.upper() in AB_DIMENSION_CODES:
        return s.upper()
    for code, name in AB_DIMENSIONS:
        if name.strip().lower() == s.lower():
            return code
    return ""


def ab_dimension_label(code):
    code = (code or "").upper()
    name = AB_DIMENSION_NAMES.get(code)
    if name and code:
        return "%s (%s)" % (name, code)
    return name or code or ""


def ab_dimension_options(code):
    if (code or "").upper() == "OC":
        return ["Response A", "Response B"]
    return list(AB_CHOICES)


def ab_flip_verdict(verdict):
    v = (verdict or "").strip()
    if v == "Response A":
        return "Response B"
    if v == "Response B":
        return "Response A"
    return v


def ab_flip_construction_keys(keys):
    return {str(c).upper(): ab_flip_verdict(v) for c, v in (keys or {}).items()}


def normalize_flaw_plan(plan):
    if not isinstance(plan, dict):
        return None
    keys = {str(c).upper(): v
            for c, v in (plan.get("construction_keys") or {}).items()}
    is_new = bool(plan.get("render_prompts") or plan.get("worker_prompt")
                  or plan.get("faithful_side"))
    if is_new:
        rp = plan.get("render_prompts") or {}
        faithful = str(plan.get("faithful_side") or "").strip().lower()
        flawed = (ab_other_side(faithful)
                  if faithful in AB_FLAWED_SIDES else "")
        planted = plan.get("planted") or {}
        return {
            "faithful_side": faithful,
            "flawed_side": flawed,
            "worker_prompt": str(plan.get("worker_prompt") or "").strip(),
            "render_prompts": {
                "a": str(rp.get("a") or "").strip(),
                "b": str(rp.get("b") or "").strip(),
            },
            "planted": {
                "a": [str(f) for f in (planted.get("a") or []) if str(f).strip()],
                "b": [str(f) for f in (planted.get("b") or []) if str(f).strip()],
            },
            "construction_keys": keys,
        }
    flawed = str(plan.get("flawed_side") or "").strip().lower()
    faithful = ab_other_side(flawed) if flawed in AB_FLAWED_SIDES else ""
    clean = str(plan.get("clean_prompt") or "").strip()
    flawed_prompt = str(plan.get("flawed_prompt") or "").strip()
    injected = [str(f) for f in (plan.get("injected_flaws") or [])
                if str(f).strip()]
    render = {"a": "", "b": ""}
    planted = {"a": [], "b": []}
    if flawed in AB_FLAWED_SIDES:
        render[flawed] = flawed_prompt
        render[faithful] = clean
        planted[flawed] = injected
    return {
        "faithful_side": faithful,
        "flawed_side": flawed,
        "worker_prompt": clean,
        "render_prompts": render,
        "planted": planted,
        "construction_keys": keys,
    }


def validate_flaw_plan(plan):
    if not isinstance(plan, dict):
        return ["flaw_plan is not an object"]
    norm = normalize_flaw_plan(plan)
    errs = []
    faithful = norm["faithful_side"]
    both_flawed = faithful not in AB_FLAWED_SIDES
    if not norm["worker_prompt"]:
        errs.append("worker_prompt (target prompt) required")
    if not norm["render_prompts"].get("a"):
        errs.append("render_prompts.a required")
    if not norm["render_prompts"].get("b"):
        errs.append("render_prompts.b required")
    if both_flawed:
        for slot in AB_FLAWED_SIDES:
            if not norm["planted"].get(slot):
                errs.append("planted flaws required on side %r (both-flawed plan)"
                            % slot)
    else:
        flawed = norm["flawed_side"]
        if flawed not in AB_FLAWED_SIDES:
            errs.append("flawed side must resolve to the 'a'/'b' slot opposite "
                        "the faithful side")
        elif not norm["planted"].get(flawed):
            errs.append("planted flaws must be a non-empty list on the flawed side")
    keys = norm["construction_keys"]
    if not keys:
        errs.append("construction_keys missing/empty")
        return errs
    missing = AB_DIMENSION_CODES - set(keys)
    if missing:
        errs.append("construction_keys missing codes %s" % sorted(missing))
    for code, _name in AB_DIMENSIONS:
        if code not in keys:
            continue
        verdict = keys[code]
        allowed = ab_dimension_options(code)
        if code == "OC" and verdict not in ("Response A", "Response B"):
            errs.append("construction_keys[OC]=%r must be DECIDED "
                        "(Response A or Response B)" % (verdict,))
            continue
        if verdict not in allowed:
            errs.append("construction_keys[%s]=%r not in %s"
                        % (code, verdict, allowed))
            continue
        if verdict == "Both Bad" and not both_flawed:
            errs.append("construction_keys[%s]='Both Bad' requires BOTH sides "
                        "flawed (faithful_side null/'both')" % (code,))
    return errs


def ab_specs_from_construction_keys(keys):
    norm = {str(c).upper(): v for c, v in (keys or {}).items()}
    specs = []
    for code, _name in AB_DIMENSIONS:
        verdict = norm.get(code)
        specs.append({
            "label": ab_dimension_label(code),
            "options": ab_dimension_options(code),
            "correct": [verdict] if verdict else [],
        })
    return specs


def parse_flaw_plan(flaw_plan_json):
    raw = (flaw_plan_json or "").strip()
    if not raw:
        return {}
    try:
        plan = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return plan if isinstance(plan, dict) else {}


def ab_construction_keys(flaw_plan_json):
    plan = parse_flaw_plan(flaw_plan_json)
    keys = plan.get("construction_keys") if isinstance(plan, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(c).upper(): v for c, v in keys.items()}


def ab_key_drift(materialized_keys, construction_keys):
    drift = []
    norm = {str(c).upper(): v for c, v in (construction_keys or {}).items()}
    for code, _name in AB_DIMENSIONS:
        expected = str(norm.get(code) or "").strip()
        got = [str(x).strip() for x in (materialized_keys.get(code) or [])]
        if not expected:
            drift.append("%s: no construction key" % code)
            continue
        if got != [expected]:
            drift.append("%s expected=%r stored=%r" % (code, expected, got))
    return drift
